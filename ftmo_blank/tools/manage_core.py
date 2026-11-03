"""MANAGE CORE: when an open position wakes its manager -- one set of rules for BOTH desks (2026-10-19).

Operator, 2026-10-19 (todos/006 sections 8 and 12): "we need another worker that only works on managing open
trades ... close, widen TP, SL to break even, partial close"; "wake it from events and feed it the actual state,
not a timer"; "wake it on pre swap so it can decide to close before the swap or roll it over"; overnight "not
managing them, only wake the worker if volatility starts to blow up -- a hard program that reads the ranges and
candle sizes compared to the previous ones".

Pure code, no broker and no Claude call: the live loop (tools/live_loop.py, every M5 tick) and the replay
(tools/paper.py, every bar) feed it the SAME inputs -- the position, its closed M5 bars since entry, recent closed
M15 bars, and (outside the window only) a few days of M5 history for the burst baseline -- and route what it
returns to the manager kind (live_manage / manage). tools/parity_check.py drives both with one synthetic market.

Triggers (each fires at most once per position, except BURST which has a cooldown and a nightly cap):
  inside the trading window
    R+0.5 / R+1 / R+1.5   the best price since entry reached that many R in favour (R = entry to the INITIAL stop)
    R-0.5                 the worst price since entry reached half the stop distance against
    INV                   a closed M15 bar closed beyond the plan's own invalidation ("INV close <98.2" in its why)
    BOS_AGAINST           a closed M15 bar since entry closed beyond the last BOS_LOOKBACK M15 bars' extreme, against
    STALL                 STALL_MIN minutes open with less than STALL_MFE_R in favour
  any time (scheduled)
    PRE_SWAP              PRE_SWAP_LEAD_MIN before the daily rollover (17:00 New York): close or roll over
    PRE_FLATTEN           Friday, PRE_FLATTEN_LEAD_MIN before the watchdog's flatten
  outside the trading window ONLY
    BURST                 the last closed M5 bar's range >= BURST_K x the median of the prior BURST_PRIOR M5 ranges AND
                          >= BURST_HOUR_K x the median range of the same UTC hour over the previous BURST_DAYS days
                          (so the routine Asia open does not wake it every night); BURST_COOLDOWN_MIN apart, at most
                          BURST_MAX_PER_NIGHT per night.
THE MANAGER'S OWN CHOICE (2026-10-20, operator: "the worker has to decide if it's important to be awake for it or
just to let it rip"): after any wake it may set ONE standing watch on its position (set_watch, the `watch` verb on
both desks) -- re-wake me if price trades through a level (WATCH_ABOVE / WATCH_BELOW) or after N minutes
(WATCH_TIME), and/or QUIET (let it rip): no automatic wake for this position (for N minutes, or until it closes)
except the Friday flatten notice. Its own watch fires at any hour, quiet or not -- it asked for it.

Measured before shipping (todos/006 section 12, feed M5 Dec 2024 - Sep 2026): a 4x burst shows on 25-67% of nights
per symbol and is followed by a >= 1-stop move within the hour 61-76% of the time -- in EITHER direction, so it is
a reason to look, not a direction.
"""
from __future__ import annotations

import datetime as dt
import re
import statistics

R_MILESTONES = (0.5, 1.0, 1.5)
ADVERSE_R = 0.5
STALL_MIN = 60
STALL_MFE_R = 0.3
BOS_LOOKBACK = 6
PRE_SWAP_LEAD_MIN = 20          # wake 20 min before the rollover ...
PRE_SWAP_LATEST_MIN = 5         # ... and not later than 5 min before it
PRE_FLATTEN_LEAD_MIN = 30
BURST_K = 4.0
BURST_PRIOR = 24
BURST_HOUR_K = 2.0
BURST_DAYS = 5
BURST_COOLDOWN_MIN = 60
BURST_MAX_PER_NIGHT = 3
BURST_FRESH_MIN = 12            # the burst bar must have closed within this many minutes
HISTORY_M5 = BURST_DAYS * 288 + BURST_PRIOR + 12   # M5 bars the burst check wants (outside the window only)
# the Friday flatten hour both desks use (tools/live_broker.py FRI_FLAT_HOUR_UTC, the watchdog's): the same env
FRI_FLAT_HOUR_UTC = int(__import__('os').environ.get('FTMO_BLANK_FRI_FLAT_HOUR_UTC', '20'))

WATCH_MIN_MIN, WATCH_MAX_MIN = 5, 24 * 60     # a timed re-wake / a quiet spell: between 5 minutes and a day
ALWAYS = ("PRE_FLATTEN",)                    # never silenced by quiet: the Friday flatten closes everything

NIGHT_ONLY = ("BURST",)
SCHEDULED = ("PRE_SWAP", "PRE_FLATTEN")

_INV_RE = re.compile(
    r"\bINV(?:ALID(?:ATION)?)?\b[^0-9<>]{0,24}?(?:(?:M5|M15|H1)\s*)?(?:close|closes|closing)?\s*"
    r"(?P<op><=?|>=?|below|under|above|over)\s*(?P<lvl>\d+(?:\.\d+)?)", re.I)


def parse_inv(text: str | None) -> dict | None:
    """The plan's own invalidation, when it is written the desk's way: 'INV close <98.2', 'INV M15 close >25580',
    'INV close above 109.70', 'invalidation: close below 1.0710'. None when the plan names none."""
    if not text:
        return None
    m = _INV_RE.search(str(text))
    if not m:
        return None
    op = m.group("op").lower()
    below = op.startswith("<") or op in ("below", "under")
    return {"op": "<" if below else ">", "level": float(m.group("lvl"))}


def _t(row: dict) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(str(row["utc"])[:16], "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
    except (KeyError, ValueError):
        return None


def _since(rows: list[dict], t0: dt.datetime, minutes: int) -> list[dict]:
    """Closed bars whose period ends after t0 (the bar the entry happened in counts)."""
    out = []
    for r in rows or []:
        t = _t(r)
        if t is not None and t + dt.timedelta(minutes=minutes) > t0 and "error" not in r:
            out.append(r)
    return out


def excursion(pos: dict, m5_since: list[dict]) -> dict:
    """Best and worst price since entry, in R of the INITIAL stop, and where the last close sits."""
    entry, sl0 = float(pos["entry"]), float(pos.get("sl0") or pos.get("sl") or 0)
    risk = abs(entry - sl0)
    if risk <= 0 or not m5_since:
        return {"risk": risk, "mfe_r": 0.0, "mae_r": 0.0, "r_now": 0.0}
    buy = pos["side"] == "buy"
    hi = max(float(b["h"]) for b in m5_since)
    lo = min(float(b["l"]) for b in m5_since)
    last = float(m5_since[-1]["c"])
    fav = (hi - entry) if buy else (entry - lo)
    adv = (entry - lo) if buy else (hi - entry)
    now = (last - entry) if buy else (entry - last)
    return {"risk": risk, "mfe_r": round(max(0.0, fav) / risk, 3), "mae_r": round(max(0.0, adv) / risk, 3),
            "r_now": round(now / risk, 3)}


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else 0.0


def burst(m5_hist: list[dict], now: dt.datetime) -> dict | None:
    """The burst read on the last closed M5 bar, or None when there is no fresh bar or not enough history."""
    timed = [(t, r) for r in (m5_hist or []) if "error" not in r and (t := _t(r)) is not None]
    if len(timed) < BURST_PRIOR + 1:
        return None
    t_last, last = timed[-1]
    if now - (t_last + dt.timedelta(minutes=5)) > dt.timedelta(minutes=BURST_FRESH_MIN):
        return None
    rng = float(last["h"]) - float(last["l"])
    prior = _median([float(r["h"]) - float(r["l"]) for _, r in timed[-(BURST_PRIOR + 1):-1]])
    lo, hi = dt.timedelta(hours=20), dt.timedelta(days=BURST_DAYS, minutes=5)
    hour = [float(r["h"]) - float(r["l"]) for t, r in timed[:-1]
            if t.hour == t_last.hour and lo <= t_last - t <= hi]
    same_hour = _median(hour)
    ratio = rng / prior if prior > 0 else 0.0
    hour_ratio = rng / same_hour if same_hour > 0 else None
    hit = (prior > 0 and ratio >= BURST_K and (hour_ratio is None or hour_ratio >= BURST_HOUR_K))
    return {"hit": hit, "bar_utc": last["utc"], "range": round(rng, 6), "ratio_prior": round(ratio, 2),
            "ratio_same_hour": round(hour_ratio, 2) if hour_ratio is not None else None,
            "direction": "up" if float(last["c"]) >= float(last["o"]) else "down", "same_hour_n": len(hour)}


def _night_key(now: dt.datetime) -> str:
    """Nights run 12:00 UTC to 12:00 UTC, so one night never splits at midnight."""
    return (now - dt.timedelta(hours=12)).date().isoformat()


def evaluate(pos: dict, m5: list[dict], m15: list[dict], now: dt.datetime, *, in_window: bool,
             rollover_utc: dt.datetime | None, friday_flat_utc: dt.datetime | None, st: dict | None = None,
             m5_hist: list[dict] | None = None, day: str | None = None) -> tuple[list[dict], dict, dict]:
    """(triggers, the position's new trigger state, the numbers the manager is shown).

    pos: side, entry, sl (current), sl0 (initial; falls back to sl), tp, opened_utc ('YYYY-MM-DD HH:MM[:SS]'),
         why (the plan text the INV is parsed from). m5/m15: closed bars (rows with utc,o,h,l,c), oldest first.
    st: what fired before for THIS position ({"fired": [...], "bursts": {night: n}, "last_burst": iso}).
    day: the trading day key for the once-a-day scheduled triggers (Prague day on both desks)."""
    st = {"fired": list((st or {}).get("fired", [])), "bursts": dict((st or {}).get("bursts", {})),
          "last_burst": (st or {}).get("last_burst"), "watch": dict((st or {}).get("watch") or {}) or None}
    fired = set(st["fired"])
    out: list[dict] = []
    try:
        t0 = dt.datetime.strptime(str(pos.get("opened_utc"))[:16], "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        t0 = now
    m5_since = _since(m5, t0, 5)
    ex = excursion(pos, m5_since)
    minutes_open = int((now - t0).total_seconds() // 60)
    info = {**ex, "minutes_open": minutes_open, "inv": parse_inv(pos.get("why"))}

    def fire(code: str, why: str, key: str | None = None):
        k = key or code
        if k in fired:
            return
        fired.add(k)
        st["fired"].append(k)
        out.append({"code": code, "why": why})

    if in_window and ex["risk"] > 0:
        for x in R_MILESTONES:
            if ex["mfe_r"] >= x:
                fire(f"R+{x:g}", f"the best price since entry reached +{ex['mfe_r']:.2f}R (now {ex['r_now']:+.2f}R)")
        if ex["mae_r"] >= ADVERSE_R:
            fire(f"R-{ADVERSE_R:g}", f"the worst price since entry reached -{ex['mae_r']:.2f}R (now {ex['r_now']:+.2f}R)")
        m15_since = _since(m15, t0, 15)
        inv = info["inv"]
        if inv and m15_since:
            c = float(m15_since[-1]["c"])
            if (c < inv["level"]) if inv["op"] == "<" else (c > inv["level"]):
                fire("INV", f"the M15 bar opening {m15_since[-1]['utc']} closed {c} {inv['op']} {inv['level']}, the "
                            "plan's own invalidation")
        t_m15 = _t(m15_since[-1]) if m15_since else None
        if t_m15 is not None:
            last = m15_since[-1]
            prior = [r for r in (m15 or []) if "error" not in r and (tr := _t(r)) is not None and tr < t_m15][-BOS_LOOKBACK:]
            if len(prior) >= BOS_LOOKBACK:
                buy = pos["side"] == "buy"
                lvl = min(float(r["l"]) for r in prior) if buy else max(float(r["h"]) for r in prior)
                c = float(last["c"])
                if (c < lvl) if buy else (c > lvl):
                    fire("BOS_AGAINST", f"the M15 bar opening {last['utc']} closed {c}, beyond the last {BOS_LOOKBACK} "
                                        f"M15 bars' {'low' if buy else 'high'} {lvl} -- structure broke against the trade")
        if minutes_open >= STALL_MIN and ex["mfe_r"] < STALL_MFE_R and ex["r_now"] > -ADVERSE_R:
            fire("STALL", f"{minutes_open} min open, best only +{ex['mfe_r']:.2f}R")

    key_day = day or now.date().isoformat()
    if friday_flat_utc is not None and dt.timedelta(minutes=PRE_SWAP_LATEST_MIN) <= friday_flat_utc - now \
            <= dt.timedelta(minutes=PRE_FLATTEN_LEAD_MIN):
        fire("PRE_FLATTEN", f"the Friday flatten closes every position at {friday_flat_utc:%H:%M} UTC "
                            f"({int((friday_flat_utc - now).total_seconds() // 60)} min)", f"PRE_FLATTEN:{key_day}")
    elif rollover_utc is not None and dt.timedelta(minutes=PRE_SWAP_LATEST_MIN) <= rollover_utc - now \
            <= dt.timedelta(minutes=PRE_SWAP_LEAD_MIN):
        fire("PRE_SWAP", f"the daily rollover (17:00 New York, swap charged, spreads spike) is at {rollover_utc:%H:%M} "
                         f"UTC ({int((rollover_utc - now).total_seconds() // 60)} min)", f"PRE_SWAP:{key_day}")

    w = st.get("watch") or {}
    quiet = bool(w.get("quiet")) and (not w.get("quiet_until") or now < _iso(w["quiet_until"], now))
    if w.get("quiet") and not quiet:
        w["quiet"], w["quiet_until"] = False, None          # the quiet spell is over: back to the normal wakes
    if not in_window and m5_hist and not quiet:
        b = burst(m5_hist, now)
        info["burst"] = b
        night = _night_key(now)
        last_b = None
        if st.get("last_burst"):
            try:
                last_b = dt.datetime.fromisoformat(st["last_burst"])
            except ValueError:
                last_b = None
        cool = last_b is None or now - last_b >= dt.timedelta(minutes=BURST_COOLDOWN_MIN)
        if b and b["hit"] and cool and st["bursts"].get(night, 0) < BURST_MAX_PER_NIGHT:
            st["bursts"][night] = st["bursts"].get(night, 0) + 1
            st["last_burst"] = now.isoformat()
            out.append({"code": "BURST", "why": (
                f"the M5 bar opening {b['bar_utc']} ran {b['range']} ({b['direction']}): {b['ratio_prior']}x the prior "
                f"{BURST_PRIOR} bars' median range" + (f", {b['ratio_same_hour']}x this hour's usual range over the last "
                                                        f"{BURST_DAYS} days" if b['ratio_same_hour'] is not None else ""))})
    if quiet:   # the manager chose to let it rip: what would have woken it is acknowledged (marked fired), not sent
        info["silenced"] = [x["code"] for x in out if x["code"] not in ALWAYS]
        out = [x for x in out if x["code"] in ALWAYS]
    # its own standing watch fires at any hour, quiet or not
    if w:
        t_set = _iso(w.get("set_at"), now)
        after = [r for r in m5_since if (tr := _t(r)) is not None and tr + dt.timedelta(minutes=5) > t_set]
        if w.get("above") is not None and after and max(float(r["h"]) for r in after) >= float(w["above"]):
            out.append({"code": "WATCH_ABOVE", "why": f"price traded through {w['above']}, the level you asked to be "
                                                      f"woken at ({w.get('why', '')})"})
            w["above"] = None
        if w.get("below") is not None and after and min(float(r["l"]) for r in after) <= float(w["below"]):
            out.append({"code": "WATCH_BELOW", "why": f"price traded through {w['below']}, the level you asked to be "
                                                      f"woken at ({w.get('why', '')})"})
            w["below"] = None
        if w.get("wake_at") and now >= _iso(w["wake_at"], now):
            out.append({"code": "WATCH_TIME", "why": f"the re-wake you set for {w['wake_at'][:16]} UTC ({w.get('why', '')})"})
            w["wake_at"] = None
        live = any(w.get(k) for k in ("above", "below", "wake_at")) or w.get("quiet")
        st["watch"] = w if live else None
    info["watch"] = st.get("watch")
    info["triggers"] = [x["code"] for x in out]
    return out, st, info


def _iso(text, default: dt.datetime) -> dt.datetime:
    try:
        t = dt.datetime.fromisoformat(str(text))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return default


def set_watch(st: dict | None, now: dt.datetime, *, side: str, price: float | None, above: float | None = None,
              below: float | None = None, in_min: int | None = None, quiet: bool = False,
              quiet_min: int | None = None, clear: bool = False, why: str = "") -> tuple[dict, dict]:
    """The manager's standing watch on its position (both desks' `watch` verb): (new trigger state, reply).
    One watch per position; a new one replaces the old. price = the position's current price (bars are bid)."""
    st = dict(st or {})
    if clear:
        st["watch"] = None
        return st, {"ok": True, "watch": None, "note": "cleared: the normal wakes apply again"}
    why = (why or "").strip()
    if len(why) < 10:
        return st, {"ok": False, "reason": "say why in --why (>= 10 characters): what the level or the time means"}
    if above is None and below is None and in_min is None and not quiet:
        return st, {"ok": False, "reason": "give --above P, --below P, --in MIN and/or --quiet [--for MIN]"}
    if above is not None and price is not None and above <= price:
        return st, {"ok": False, "reason": f"price {price} is already at or above {above}"}
    if below is not None and price is not None and below >= price:
        return st, {"ok": False, "reason": f"price {price} is already at or below {below}"}
    for name, v in (("--in", in_min), ("--for", quiet_min)):
        if v is not None and not (WATCH_MIN_MIN <= int(v) <= WATCH_MAX_MIN):
            return st, {"ok": False, "reason": f"{name} must be {WATCH_MIN_MIN}-{WATCH_MAX_MIN} minutes"}
    w = {"set_at": now.isoformat(), "why": why[:300], "above": above, "below": below,
         "wake_at": (now + dt.timedelta(minutes=int(in_min))).isoformat() if in_min else None,
         "quiet": bool(quiet), "quiet_until": (now + dt.timedelta(minutes=int(quiet_min))).isoformat()
         if (quiet and quiet_min) else None}
    st["watch"] = w
    return st, {"ok": True, "watch": w}


def rollover_utc(now: dt.datetime) -> dt.datetime:
    """The next daily rollover: 17:00 New York (the broker's midnight), as an aware UTC datetime."""
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    local = now.astimezone(ny)
    t = local.replace(hour=17, minute=0, second=0, microsecond=0)
    if t <= local:
        t += dt.timedelta(days=1)
    return t.astimezone(dt.timezone.utc)


def friday_flat_utc(now: dt.datetime, hour_utc: int) -> dt.datetime | None:
    """Today's Friday flatten (the watchdog's FRI_FLAT_HOUR_UTC) if today is a Friday and it is still ahead."""
    if now.weekday() != 4:
        return None
    t = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    return t if t > now else None


def describe(pos: dict, info: dict, triggers: list[dict]) -> str:
    """The position block of the manager's prompt (both desks, word for word)."""
    inv = info.get("inv")
    lines = [
        f"POSITION {pos.get('ticket', pos.get('id'))}: {pos['symbol']} {pos['side']} {pos.get('lots')} lots, entry "
        f"{pos['entry']}, stop now {pos.get('sl')} (initial {pos.get('sl0') or pos.get('sl')}), target {pos.get('tp')}, "
        f"opened {pos.get('opened_utc')} ({info.get('minutes_open')} min ago).",
        f"  R = {round(info.get('risk', 0), 6)} (entry to the initial stop). Best since entry +{info.get('mfe_r', 0):.2f}R, "
        f"worst -{info.get('mae_r', 0):.2f}R, last close {info.get('r_now', 0):+.2f}R"
        + (f", floating {pos.get('floating')}" if pos.get("floating") is not None else "") + ".",
        f"  The plan it was opened on: {pos.get('why') or '(no rationale recorded)'}",
        "  The plan's invalidation, as code reads it: " + (f"an M15 close {inv['op']} {inv['level']}" if inv else
                                                          "none written in the INV form"),
        "  Your standing watch on it: " + _watch_text(info.get("watch")),
        "WHY YOU WERE WOKEN (code triggers, each fires once per position):"]
    lines += [f"  {x['code']}: {x['why']}" for x in triggers]
    return "\n".join(lines)


def _watch_text(w: dict | None) -> str:
    if not w:
        return "none (the normal wakes apply)"
    bits = [f"wake above {w['above']}" if w.get("above") is not None else "",
            f"wake below {w['below']}" if w.get("below") is not None else "",
            f"wake at {str(w['wake_at'])[:16]} UTC" if w.get("wake_at") else "",
            ("QUIET" + (f" until {str(w['quiet_until'])[:16]} UTC" if w.get("quiet_until") else " until it closes"))
            if w.get("quiet") else ""]
    return "; ".join(b for b in bits if b) + f" -- {w.get('why', '')}"


# ---- THE ONCE-A-DAY SWING TURN (operator, 2026-10-20: "bring back the once a day swing decision ON TOP of everything
# ... the same as where it was turned before") -----------------------------------------------------------------------
# The old training turn ran at 22:45 broker time (19:45 UTC in New York summer, 20:45 UTC in winter -- turns.jsonl), i.e.
# 15:45 New York, 75 minutes before the rollover, after the day's move. Its era (attempts 3-8): 20 trades, 60% won,
# +0.56R mean, held ~17h, mostly gold. Monday-Thursday: on Friday the flatten (20:00 UTC) would close it within minutes.
SWING_NY = tuple(int(x) for x in __import__("os").environ.get("FTMO_BLANK_SWING_NY", "15:45").split(":"))
SWING_DAYS = (0, 1, 2, 3)
SWING_LATE_MIN = 60          # a tick up to an hour late still takes the turn (a restart, a slow tick); later, skip it


def swing_key(now: dt.datetime) -> str | None:
    """The swing turn's day key (New York date) when `now` is inside its slot, else None."""
    from zoneinfo import ZoneInfo
    ny = now.astimezone(ZoneInfo("America/New_York"))
    t = ny.replace(hour=SWING_NY[0], minute=SWING_NY[1], second=0, microsecond=0)
    if ny.weekday() not in SWING_DAYS or not (t <= ny < t + dt.timedelta(minutes=SWING_LATE_MIN)):
        return None
    return ny.date().isoformat()


def day_movers(rows_by_symbol: dict, day_start_utc: dt.datetime, limit: int = 8) -> list[dict]:
    """Each symbol's move since the trading day began (first M5 open of the day to the last close), biggest first.
    rows_by_symbol: symbol -> closed M5 rows (utc, o, h, l, c)."""
    out = []
    for sym, rows in rows_by_symbol.items():
        today = [r for r in rows or [] if "error" not in r and (t := _t(r)) is not None and t >= day_start_utc]
        if len(today) < 12 or float(today[0]["o"]) <= 0:
            continue
        pct = (float(today[-1]["c"]) - float(today[0]["o"])) / float(today[0]["o"]) * 100
        hi, lo = max(float(r["h"]) for r in today), min(float(r["l"]) for r in today)
        pos = (float(today[-1]["c"]) - lo) / (hi - lo) if hi > lo else 0.5
        out.append({"symbol": sym, "pct_day": round(pct, 3), "close_in_range": round(pos, 2)})
    out.sort(key=lambda x: -abs(x["pct_day"]))
    return out[:limit]
