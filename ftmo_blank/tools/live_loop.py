"""LIVE LOOP: what one wall-clock tick of the LIVE mode does. driver.py live-tick / live-run call
tick(); this module only reads the broker and decides which EVENTS happened -- turning events into
Claude demands (and paying for them) is driver.py's job, the same split TRAINING has between
paper.py advance() (produces the outbox) and driver.py turn() (routes it).

One tick:
  1. snapshot the real account (tools/live_broker.py snapshot) and publish it (tools/market_feed.py)
  2. reconcile this worker's own positions against the broker's deal history: a position that is
     gone AND has an exit deal is a closed trade (a stop or target hit AT the broker included) ->
     live_trade_closed, also written to state/live_ledger.jsonl as type trade_closed
  3. a Prague-midnight rollover since the last tick -> live_day_closed for the day that ended
  4. the account's FTMO daily/total loss past WARN_FRACTION of its limit -> live_risk_warning (once a day)
  5. this worker's own halt newly tripped -> live_halt (recorded, not routed)
  6. inside the TRADING WINDOW (weekdays 07:00-20:00 Europe/Madrid by default, DST-aware), at the
     start of each decision slot (every DECISION_EVERY_MIN, default 60) -> live_reassess, carrying the
     top movers of the last hour so the decider does not have to screen 34 symbols itself
  7. inside the window, on EVERY other tick (each M5 close): paper.py's pure-code move screen
     (12 closed M5 bars, >= 0.30%) -> live_setup_candidate for symbols not flagged yet today. Free
     until it hits; a hit wakes a scout, which can hand the symbol to a trader or the decider at once
  8. the first tick at or after the window's end on a weekday -> live_risk_review for the live_risk
     worker (the one kind that may move the live risk lever), if trades closed that day, and every
     Friday regardless (weekly checkpoint)

  9. the same Prague-day rollover (3) also gathers that day's live PROCESS lessons -- live board
     topics lesson / day_review / risk, never pre_trade or scan posts, which carry levels -- and
     wakes live_dream to distil them into DREAMS for the training replay (tools/dreams.py)

Outside the window nothing wakes a trading kind on schedule: positions stay protected by their
broker-side stops and targets and by tools/live_watchdog.py; closed trades are still reviewed.

Safety rule carried over from tools/live_watchdog.py: an empty or failed positions read is never
taken to mean "everything closed". A position only counts as closed when the broker shows its exit
deal; until then it stays in the seen-set and is retried next tick.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from guard import rules as R  # noqa: E402
import live_broker as LB  # noqa: E402
import live_market as LM  # noqa: E402
import desk_shared as DS  # noqa: E402  (collapse_twins: the same screen rule as paper.py)

PRAGUE = ZoneInfo(R.DAY_TZ)
SCAN_LOOKBACK_M5 = 12        # paper.py SCAN_EVERY_BARS: one hour of M5 bars
SCAN_THRESHOLD_PCT = 0.30    # paper.py SCAN_THRESHOLD_PCT
SCAN_MAX = 5
# Operator, 2026-10-12: a symbol that triggered the scout may trigger it AGAIN once this many minutes
# have passed since it last did (before: once per Prague day, so after a busy morning the screen went
# quiet until the next decision turn). The screen still needs a fresh >= SCAN_THRESHOLD_PCT move over
# its own last hour, so a re-trigger is a new move, not the same one seen twice. 0 = once per day.
REFLAG_AFTER_MIN = int(os.environ.get("FTMO_BLANK_LIVE_REFLAG_AFTER_MIN", "60"))
RECENT_EVENTS_MAX = 30
DEAL_ENTRY_OUT = (1, 3)      # DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY
DEAL_REASON = {0: "operator_manual", 1: "operator_manual", 2: "operator_manual", 3: "expert", 4: "sl", 5: "tp",
               6: "stop_out"}
UNRESOLVED_GIVE_UP_DAYS = 7


def _parse_window(text: str) -> tuple[int, int]:
    a, b = text.split("-", 1)
    to_min = lambda hm: int(hm.split(":")[0]) * 60 + int(hm.split(":")[1])
    return to_min(a.strip()), to_min(b.strip())


# Operator, 2026-10-11: free to trade from ~07:00 to 20:00 Madrid time. Kept in local time on
# purpose so the window follows Madrid's own DST switch (05:00-18:00 UTC in summer, 06:00-19:00 in
# winter) instead of drifting by an hour twice a year.
WINDOW_TZ = ZoneInfo(os.environ.get("FTMO_BLANK_LIVE_WINDOW_TZ", "Europe/Madrid"))
WINDOW_START_MIN, WINDOW_END_MIN = _parse_window(os.environ.get("FTMO_BLANK_LIVE_WINDOW", "07:00-20:00"))
DECISION_EVERY_MIN = int(os.environ.get("FTMO_BLANK_LIVE_DECISION_EVERY_MIN", "60"))
MOVERS_IN_DECISION = 8


def _local_minutes(now: dt.datetime) -> tuple[dt.datetime, int]:
    local = now.astimezone(WINDOW_TZ)
    return local, local.hour * 60 + local.minute


def in_window(now: dt.datetime) -> bool:
    local, mins = _local_minutes(now)
    return local.weekday() < 5 and WINDOW_START_MIN <= mins < WINDOW_END_MIN


def decision_slot(now: dt.datetime) -> str | None:
    """Which decision slot `now` falls in (e.g. '2026-10-07#3' = the 4th of the day), or None outside
    the window. One live_reassess fires per slot, on the first tick inside it."""
    if not in_window(now):
        return None
    local, mins = _local_minutes(now)
    return f"{local.date().isoformat()}#{(mins - WINDOW_START_MIN) // DECISION_EVERY_MIN}"


def next_decision_utc(now: dt.datetime) -> str | None:
    local, mins = _local_minutes(now)
    nxt = WINDOW_START_MIN + ((mins - WINDOW_START_MIN) // DECISION_EVERY_MIN + 1) * DECISION_EVERY_MIN
    if not in_window(now) or nxt >= WINDOW_END_MIN:
        return None  # the next turn is tomorrow's (or Monday's) first slot
    t = local.replace(hour=nxt // 60, minute=nxt % 60, second=0, microsecond=0)
    return t.astimezone(dt.timezone.utc).strftime("%H:%M UTC")


def window_text() -> str:
    hm = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    return (f"weekdays {hm(WINDOW_START_MIN)}-{hm(WINDOW_END_MIN)} {WINDOW_TZ.key} time, a decision turn every "
            f"{DECISION_EVERY_MIN} min, the move screen on every 5-minute bar in between")


def _state_path(home) -> Path:
    return Path(home) / "state" / "live_loop.json"


def _seen_path(home) -> Path:
    return Path(home) / "state" / "live_positions_seen.json"


def _read(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    os.replace(tmp, path)


def _prague_day(t: dt.datetime) -> str:
    return t.astimezone(PRAGUE).date().isoformat()


# ---- 2. closed-trade reconciliation ----------------------------------------------------------
def _closed_trade(m, ticket: int, seen: dict | None, placed: dict | None) -> dict | None:
    """The closed-trade record for one of this worker's positions, from the broker's own deals,
    or None if the broker shows no exit for it yet."""
    deals = m.history_deals_get(position=int(ticket))
    if not deals:
        return None
    outs = [d for d in deals if int(getattr(d, "entry", -1)) in DEAL_ENTRY_OUT]
    if not outs:
        return None
    ins = [d for d in deals if int(getattr(d, "entry", -1)) == 0]
    seen, placed = seen or {}, placed or {}
    vol = sum(float(d.volume) for d in outs) or 1.0
    exit_px = sum(float(d.price) * float(d.volume) for d in outs) / vol
    net = sum(float(getattr(d, "profit", 0) or 0) + float(getattr(d, "commission", 0) or 0)
              + float(getattr(d, "swap", 0) or 0) + float(getattr(d, "fee", 0) or 0) for d in deals)
    last = max(outs, key=lambda d: int(d.time))
    reason = DEAL_REASON.get(int(getattr(last, "reason", -1)), "unknown")
    comment = str(getattr(last, "comment", "") or "")
    if reason == "expert":
        reason = "watchdog_flatten" if "fri_flat" in comment else "manual"
    side = placed.get("side") or seen.get("side") or ("buy" if ins and int(ins[0].type) == 0 else "sell")
    entry = placed.get("entry") or seen.get("entry") or (float(ins[0].price) if ins else None)
    sl0 = placed.get("sl") or seen.get("sl")
    risk = placed.get("risk_ccy")
    closed_utc = R.broker_to_utc(int(last.time))
    return {
        "ticket": int(ticket), "symbol": placed.get("symbol") or seen.get("symbol") or last.symbol, "side": side,
        "lots": placed.get("lots") or seen.get("lots") or vol, "entry": entry, "exit": round(exit_px, 6),
        "initial_sl": sl0, "tp": placed.get("tp") or seen.get("tp"), "reason": reason,
        "net": round(net, 2), "currency": placed.get("currency"),
        "risk_ccy": risk, "r_net": round(net / risk, 3) if risk else None,
        "r_price": R.chart_r_multiple("long" if side == "buy" else "short", entry, round(exit_px, 6), sl0),
        "result": "win" if net > 0 else ("loss" if net < 0 else "flat"),
        "opened_utc": placed.get("utc") or seen.get("opened_utc"), "closed_utc": closed_utc.strftime("%Y-%m-%d %H:%M:%S"),
        "day": _prague_day(closed_utc), "why_open": placed.get("why"),
        "consulted_analysis": placed.get("consulted_analysis", False),
        "ref_price": placed.get("ref_price"),
    }


def reconcile(m, home=HOME, now: dt.datetime | None = None) -> list[dict]:
    """New and closed positions of THIS worker since the last tick. Candidates for "closed" are
    every ticket seen open before AND every ticket this tool placed that has no trade_closed row
    yet -- the second set catches a position that opened and closed between two ticks."""
    now = now or LB._now_utc()
    seen = _read(_seen_path(home), {})
    rows = LB.ledger_rows(home=home)
    done = {int(r["ticket"]) for r in rows if r.get("type") == "trade_closed" and r.get("ticket") is not None}
    placed = {int(r["ticket"]): r for r in rows if r.get("type") == "order_placed" and r.get("ticket")}
    own = LB._own_positions_raw(m)  # raises on a failed read: the tick reports it, nothing is evicted
    open_now = {}
    for p in own:
        open_now[str(int(p.ticket))] = {
            "symbol": p.symbol, "side": LB._position_side(p), "lots": float(p.volume), "entry": float(p.price_open),
            "sl": float(getattr(p, "sl", 0) or 0) or None, "tp": float(getattr(p, "tp", 0) or 0) or None,
            "opened_utc": R.broker_to_utc(int(p.time)).strftime("%Y-%m-%d %H:%M:%S") if getattr(p, "time", None) else None}
    events = []
    for tk, rec in open_now.items():
        if tk not in seen:
            seen[tk] = rec
            events.append({"type": "live_trade_opened", "ticket": int(tk), **rec})
    candidates = {int(tk) for tk in seen if tk not in open_now}
    for tk, pr in placed.items():
        if tk in done or str(tk) in open_now:
            continue
        try:
            placed_at = dt.datetime.strptime(pr.get("utc", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            placed_at = None
        if placed_at is None or now - placed_at < dt.timedelta(days=UNRESOLVED_GIVE_UP_DAYS):
            candidates.add(tk)
    for tk in sorted(candidates - done):
        rec = _closed_trade(m, tk, seen.get(str(tk)), placed.get(tk))
        if rec is None:
            continue  # no exit deal yet: not closed as far as the broker says; try again next tick
        # every stop this worker moved on the position (2026-10-12): without them the trade review read
        # initial_sl -> exit and called a twice-trailed trade "no trail"
        rec["stop_moves"] = [{"utc": r.get("utc") or r.get("logged_utc"), "old_sl": r.get("old_sl"),
                              "new_sl": r.get("new_sl"), "why": r.get("why")}
                             for r in rows if r.get("type") == "modify_placed" and str(r.get("id")) == str(tk)]
        LB.log("trade_closed", **rec)
        done.add(tk)
        seen.pop(str(tk), None)
        events.append({"type": "live_trade_closed", **rec})
    _write(_seen_path(home), seen)
    return events


# ---- 7. the hourly move screen ------------------------------------------------------------------
def scan_candidates(m, now: dt.datetime, flagged: list[str], threshold_pct: float = SCAN_THRESHOLD_PCT,
                    limit: int = SCAN_MAX) -> list[dict]:
    """paper.py scan_candidates() on live bars: who moved >= SCAN_THRESHOLD_PCT over the last
    SCAN_LOOKBACK_M5 closed M5 bars. A symbol whose last closed bar is over 15 minutes old (its
    market is shut) is skipped, not compared against a stale price."""
    now_b = LB.utc_to_broker_epoch(now)
    out = []
    for sym in R.INSTRUMENTS:
        if sym in flagged:
            continue
        try:
            rates = m.copy_rates_from_pos(sym, m.TIMEFRAME_M5, 0, SCAN_LOOKBACK_M5 + 2)
        except Exception:
            continue
        if rates is None or not len(rates):
            # no bars at all = most likely not active in this account's Market Watch (a fresh account
            # starts on the broker's default list): activate it now so the next tick can screen it,
            # instead of skipping it silently on every tick (2026-10-12)
            try:
                LB.ensure_symbol(m, sym, wait_s=0)
            except Exception:
                pass
            continue
        bars = LM._closed(rates, 300, now_b)
        if len(bars) < SCAN_LOOKBACK_M5 + 1 or now_b - bars[-1]["t"] > 20 * 60:
            continue
        window = bars[-(SCAN_LOOKBACK_M5 + 1):]
        start, end = window[0]["c"], window[-1]["c"]
        if start <= 0:
            continue
        pct = (end - start) / start * 100
        if abs(pct) >= threshold_pct:
            out.append({"symbol": sym, "pct_move": round(pct, 3), "bars": SCAN_LOOKBACK_M5})
    out.sort(key=lambda c: -abs(c["pct_move"]))
    return out[:limit]


# ---- 3. the day that ended ------------------------------------------------------------------
DREAM_TOPICS = ("lesson", "day_review", "risk")
DREAM_MAX_POSTS = 15
DREAM_MAX_CHARS = 600


def day_lessons(day: str, home=HOME) -> list[str]:
    """The live board's process lessons of one Prague day, newest last, each cut to DREAM_MAX_CHARS --
    the raw material live_dream distils. Raw on purpose: the levels and dates in them are what the
    dream checks exist to strip, and live_dream is told so."""
    try:
        import live_board
        rows = live_board.read(n=0, home=home)
    except Exception:
        return []
    picked = [r for r in rows if r.get("day") == day
              and any(t in str(r.get("topic", "")).lower() for t in DREAM_TOPICS)]
    return [str(r.get("text", ""))[:DREAM_MAX_CHARS] for r in picked][-DREAM_MAX_POSTS:]


def day_record(day: str, home=HOME) -> dict:
    rows = LB.ledger_rows(home=home)
    closed = [r for r in rows if r.get("type") == "trade_closed" and r.get("day") == day]
    placed = [r for r in rows if r.get("type") == "order_placed" and r.get("day") == day]
    notes = [r for r in rows if r.get("type") == "journal" and r.get("day") == day]
    return {"day": day, "own_realized": round(sum(float(r.get("net") or 0) for r in closed), 2),
            "closed_trades": len(closed), "orders_placed": len(placed), "traded": bool(placed),
            "journal_notes": len(notes), "results": [r.get("result") for r in closed],
            "currency": (closed or placed or [{}])[0].get("currency")}


# ---- the position manager's wake -----------------------------------------------------------------
def _manage_path(home) -> Path:
    return Path(home) / "state" / "live_manage.json"


def manage_events(m, snap: dict, now: dt.datetime, home=HOME) -> list[dict]:
    """live_manage events for this assembly's own open positions (snapshot own_positions: MAGIC only), by the
    shared rules of tools/manage_core.py. Per-position trigger state lives in state/live_manage.json."""
    import manage_core as MC
    own = [p for p in (snap.get("own_positions") or []) if p.get("ticket")]
    path = _manage_path(home)
    st_all = _read(path, {})
    st_all = {k: v for k, v in st_all.items() if k in {str(p["ticket"]) for p in own}}   # closed ones drop out
    if not own or not snap.get("market_open"):
        _write(path, st_all)
        return []
    placed = {str(r.get("ticket")): r for r in LB.ledger_rows(home=home) if r.get("type") == "order_placed"}
    inw = in_window(now)
    roll = MC.rollover_utc(now)
    fri = MC.friday_flat_utc(now, MC.FRI_FLAT_HOUR_UTC)
    out = []
    for p in own:
        tk = str(p["ticket"])
        pl = placed.get(tk) or {}
        opened = str(p.get("opened_utc") or pl.get("utc") or "")
        try:
            t0 = dt.datetime.strptime(opened[:16], "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        n5 = min(LM.MAX_BARS, max(3, int((now - t0).total_seconds() // 300) + 3))
        m5 = LM.bars(p["symbol"], n5, "M5", m=m)
        m15 = LM.bars(p["symbol"], min(LM.MAX_BARS, MC.BOS_LOOKBACK + n5 // 3 + 3), "M15", m=m)
        hist = None if inw else LM.m5_history(p["symbol"], MC.HISTORY_M5, m=m)
        pos = {"ticket": p["ticket"], "symbol": p["symbol"], "side": p["side"], "lots": p.get("lots"),
               "entry": p["entry"], "sl": p.get("sl"), "sl0": pl.get("sl") or p.get("sl"), "tp": p.get("tp"),
               "opened_utc": opened[:16], "why": pl.get("why"), "floating": p.get("floating")}
        if not pos["sl0"]:
            continue   # no stop, no R: nothing to measure the trade by (the order gate never lets one through)
        trig, st, info = MC.evaluate(pos, m5, m15, now, in_window=inw, rollover_utc=roll, friday_flat_utc=fri,
                                     st=st_all.get(tk), m5_hist=hist, day=_prague_day(now))
        st_all[tk] = st
        if trig:
            out.append({"type": "live_manage", "at_utc": now.strftime("%Y-%m-%d %H:%M"), **pos, "triggers": trig,
                        "info": info})
    _write(path, st_all)
    return out


def _m5_rows(m, sym: str, n: int, now: dt.datetime) -> list[dict]:
    """Closed M5 rows read straight from the terminal, like the move screen: no activation, no retry wait (a symbol
    with no bars is simply skipped; 34 of them must not cost minutes)."""
    try:
        rates = m.copy_rates_from_pos(sym, m.TIMEFRAME_M5, 0, n + 1)
    except Exception:
        return []
    if rates is None or not len(rates):
        return []
    return [LM._row(b, "M5", "") for b in LM._closed(rates, 300, LB.utc_to_broker_epoch(now))][-n:]


def swing_event(m, snap: dict, now: dt.datetime, st: dict) -> dict | None:
    """live_swing once per New York day in its slot, with the day's movers (first M5 open of the Prague day to now)."""
    import manage_core as MC
    key = MC.swing_key(now)
    if not key or st.get("swing_day") == key or not snap.get("market_open") or snap.get("entry_block"):
        return None
    st["swing_day"] = key
    day_start = now.astimezone(PRAGUE).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc)
    rows = {sym: _m5_rows(m, sym, 288, now) for sym in R.INSTRUMENTS}
    return {"type": "live_swing", "key": key, "movers": MC.day_movers(rows, day_start),
            "recent_events": st.get("recent_events", [])}


# ---- the tick ----------------------------------------------------------------------------------
_BRIEF_KEYS = {
    "live_trade_closed": ("ticket", "symbol", "side", "reason", "result", "r_price", "net", "closed_utc"),
    "live_trade_opened": ("ticket", "symbol", "side", "lots", "entry", "sl"),
    "live_setup_candidate": ("candidates",),
    "live_day_closed": ("day", "own_realized", "closed_trades", "orders_placed"),
    "live_risk_warning": ("kind", "used_pct_of_limit"),
    "live_halt": ("reason",),
    "live_level_alert": ("id", "symbol", "condition", "touched_utc"),
    "live_alert_error": ("reason",),
    "live_manage": ("ticket", "symbol", "triggers"),
    "live_manage_error": ("reason",),
    "live_swing": ("key", "movers"),
    "live_vault_review": ("day",),
}


def _brief(e: dict) -> dict:
    """What the next decision turn needs to know about an event, not its whole record -- 30 full
    trade records would crowd the 20k-character prompt cap on their own."""
    keys = _BRIEF_KEYS.get(e["type"], ())
    return {"type": e["type"], **{k: e.get(k) for k in keys}}


def _live_flags(st: dict, now: dt.datetime) -> list[str]:
    """st["flagged"] minus every symbol flagged >= REFLAG_AFTER_MIN ago (st["flagged_at"] keeps when).
    A flag carried over from before flag times were kept starts its hour now: never sooner than the
    old once-a-day rule would have allowed."""
    flagged = st.setdefault("flagged", [])
    at = st.setdefault("flagged_at", {})
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    for s in flagged:
        at.setdefault(s, stamp)
    if REFLAG_AFTER_MIN > 0:
        cutoff = now - dt.timedelta(minutes=REFLAG_AFTER_MIN)
        flagged[:] = [s for s in flagged
                      if dt.datetime.strptime(at[s], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc) > cutoff]
    st["flagged_at"] = {s: at[s] for s in flagged}
    return flagged


def _flag(st: dict, symbols, now: dt.datetime, restart: bool = False) -> None:
    """Flag symbols now. restart=True also restarts the hour of one already flagged: a decision turn
    just showed it to the decider, so the scout should not re-handle that same move minutes later."""
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    for s in symbols:
        if s not in st["flagged"]:
            st["flagged"].append(s)
            st["flagged_at"][s] = stamp
        elif restart:
            st["flagged_at"][s] = stamp


def tick(m=None, now: dt.datetime | None = None, home=HOME) -> dict:
    """One tick. Returns {"snapshot", "events": [event dict with "type"], "error"?}. Never raises
    for a broker problem: an unreachable bridge is a tick with an error and no events."""
    now = now or LB._now_utc()
    st = _read(_state_path(home), {})
    try:
        m = m or LB._mt5_client()
        snap = LB.snapshot(m)
    except Exception as exc:
        return {"snapshot": None, "events": [], "error": f"snapshot failed: {exc}"}
    try:
        import market_feed
        market_feed.distribute("LIVE", home=home, snapshot=snap)
    except Exception:
        pass  # publishing is a convenience for readers; the tick itself does not depend on it
    events: list[dict] = []
    try:
        events += reconcile(m, home=home, now=now)
    except Exception as exc:
        events.append({"type": "live_halt", "reason": f"reconcile failed this tick (retried next tick): {exc}"})

    today = _prague_day(now)
    if st.get("day") and st["day"] != today:
        events.append({"type": "live_day_closed", **day_record(st["day"], home=home)})
        # the dream waits ONE tick (2026-10-12): the day-close review above runs in THIS tick's drain and
        # posts the day_review that DREAM_TOPICS names -- read now, the dream could never include it
        st["dream_day"] = st["day"]
        st["flagged"], st["flagged_at"], st["warned"] = [], {}, []
    elif st.get("dream_day"):
        lessons = day_lessons(st["dream_day"], home=home)
        if lessons:
            events.append({"type": "live_dream", "lessons": lessons})
        # the vault's value extraction (2026-10-20): once a day, after the day review has posted
        events.append({"type": "live_vault_review", "day": st["dream_day"]})
        st["dream_day"] = None
    st["day"] = today

    warned = st.setdefault("warned", [])
    for kind, used, limit in (("daily", snap["daily_loss_used"], snap["daily_loss_limit"]),
                              ("total", snap["total_loss_used"], snap["total_loss_limit"])):
        if limit > 0 and used >= R.WARN_FRACTION * limit and kind not in warned:
            warned.append(kind)
            events.append({"type": "live_risk_warning", "kind": kind, "used": used, "limit": limit,
                           "used_pct_of_limit": round(100 * used / limit, 1)})

    # price alerts (2026-10-18, tools/live_alerts.py): every bar that closed since the last tick is judged by
    # code; a condition met inside the window wakes that symbol's trader once, with its plan
    try:
        import live_alerts
        events += live_alerts.check(m, now, in_window, home=home)
    except Exception as exc:
        events.append({"type": "live_alert_error", "reason": f"alert check failed this tick (retried next tick): {exc}"})

    # the position manager's wake (2026-10-19, tools/manage_core.py -- the replay runs the same rules): this assembly's
    # OWN open positions only (the snapshot's own_positions = MAGIC), from bars this tick reads anyway; no Claude call
    # unless a trigger fires, and outside the window only a volatility burst (or the scheduled pre-swap) can wake it
    try:
        events += manage_events(m, snap, now, home=home)
    except Exception as exc:
        events.append({"type": "live_manage_error", "reason": f"manager check failed this tick (retried next tick): {exc}"})

    # the once-a-day swing turn (2026-10-20, tools/manage_core.py swing_key: 15:45 New York, Monday-Thursday)
    try:
        ev = swing_event(m, snap, now, st)
        if ev:
            events.append(ev)
    except Exception as exc:
        events.append({"type": "live_manage_error", "reason": f"swing turn failed this tick: {exc}"})

    if snap.get("own_halt") and snap["own_halt"] != st.get("halt"):
        events.append({"type": "live_halt", "reason": snap["own_halt"]})
    st["halt"] = snap.get("own_halt")

    # this tick's events so far join the running list BEFORE a decision turn takes it, so a trade
    # that closed in the same tick as a decision hour is in that decision's events, not the next one's
    if events:
        st["recent_events"] = (st.get("recent_events", []) + [_brief(e) for e in events])[-RECENT_EVENTS_MAX:]

    open_now = in_window(now) and snap.get("market_open")
    slot = decision_slot(now)
    flagged = _live_flags(st, now)
    if open_now and slot != st.get("decided_slot"):
        st["decided_slot"] = slot
        movers = scan_candidates(m, now, [], threshold_pct=0.0, limit=MOVERS_IN_DECISION)
        _flag(st, [c["symbol"] for c in movers if abs(c["pct_move"]) >= SCAN_THRESHOLD_PCT], now, restart=True)
        events.append({"type": "live_reassess", "recent_events": st.get("recent_events", []), "movers": movers,
                       "next_decision_utc": next_decision_utc(now), "window": window_text()})
        st["recent_events"] = []
    elif open_now and not snap.get("own_halt") and not snap.get("entry_block"):
        cands = scan_candidates(m, now, flagged)
        if cands:
            _flag(st, [c["symbol"] for c in cands], now)
            cands = DS.collapse_twins(cands)   # 2026-11-01: one correlated move wakes one scout (paper.py: the same)
            # logged like paper.py's setup_candidate row (utc = the open of the M5 bar that just closed), so the
            # rejection tracker (tools/opportunities.py) grades the same record on both desks
            bar_open = now.replace(second=0, microsecond=0) - dt.timedelta(minutes=now.minute % 5 + 5)
            LB.log("setup_candidate", utc=bar_open.strftime("%Y-%m-%d %H:%M UTC"), candidates=cands)
            ev = {"type": "live_setup_candidate", "candidates": cands, "next_decision_utc": next_decision_utc(now)}
            events.append(ev)
            st["recent_events"] = (st.get("recent_events", []) + [_brief(ev)])[-RECENT_EVENTS_MAX:]

    local, mins = _local_minutes(now)
    if local.weekday() < 5 and mins >= WINDOW_END_MIN:
        # Once at the window's end, and AGAIN whenever a trade of today closes after that (2026-10-12: a
        # USOIL target filled 8 minutes after 20:00 Madrid, the review had already looked at an empty
        # day, and the close never reached live_risk). risk_checked = {"day", "closed": count covered};
        # before this it was a bare date, i.e. a check that covered 0 closes.
        date = local.date().isoformat()
        rc = st.get("risk_checked")
        if not isinstance(rc, dict):
            rc = {"day": rc, "closed": 0}
        all_closed = LB.closed_trades(home=home)
        closed_today = [t for t in all_closed if t.get("day") == today]
        first = rc.get("day") != date
        if first or len(closed_today) > int(rc.get("closed") or 0):
            st["risk_checked"] = {"day": date, "closed": len(closed_today)}
            if closed_today or (first and local.weekday() == 4):
                events.append({"type": "live_risk_review", "day": today, "weekly": local.weekday() == 4,
                               "late": not first,
                               "closed_today": len(closed_today), "today": LB.trade_stats(closed_today),
                               "all_time": LB.trade_stats(all_closed), "last_20": LB.trade_stats(all_closed[-20:]),
                               "lever": LB.risk_factor()})
    st["last_tick_utc"] = now.strftime("%Y-%m-%d %H:%M:%S")
    _write(_state_path(home), st)
    return {"snapshot": snap, "events": events}
