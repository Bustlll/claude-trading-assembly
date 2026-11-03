"""BLINDCHEAT2: the parallel learning loop (operator, 2026-11-01). Read todos/011 first.

"let it see all the actual candles like if it was actually analyzing them like i would, and we try to extract as many lessons as
possible, and then we give it some random place in time blind, up until: trade tp hits OR, tp hits after its sl was hit."

One round:
  STUDY  a stretch it may even remember, every candle visible, future included: it marks the trades and writes what was visible
         BEFORE each entry. Code checks each marked trade on the bars; a model's claim about its own trade is never taken.
  LEARN  it rewrites its playbook from the checked study trades and the graded blind trades.
  BLIND  random moments: past candles only, the default model with the playbook AND without it, same moment. Code walks each trade
         to its target, on past a stop if it must: tp / sl_then_tp / sl / open, with how far it went against. No bar-by-bar replay.
  EXAM   every EXAM_EVERY rounds, later moments whose results never reach LEARN: the only numbers that can say the playbook helps.

Periods: STUDY is dated data the model recalls (soft cheating is fine there). BLIND and EXAM are 2026-03 onward, which it does not recall.
Touches no desk: reads feed/, writes state/blindcheat2/ only. The playbook reaches no assembly worker and no live prompt unless the
operator decides so.

Grading 2 (operator, same day: "get rid of both ... trade like live optimally ... don't throttle it"): all 34 symbols; every trade run bar by bar
with the live manager's own wakes (tools/manage_core.py) and a model call at each, at the live desk's model and effort; commission, spread and the
broker's swap (state/blindcheat2/swaps.json, today's rates); lots from a fixed $ risk, refused if under the broker's minimum. R is net money over
risk. The hands-off result of the same trade is kept beside it. No usage ceiling and no hold for live: the operator handles that. A real limit moves
to the other login, or waits for one.

Saved for replay: decisions.jsonl (every level, wake, action and exit), prompts.jsonl (every prompt and answer, word for word).

  blindcheat2.py run | once | status | show ID | replay
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME))
sys.path.insert(0, str(HOME / "tools"))
ST = HOME / "state" / "blindcheat2"
DECISIONS, STUDIES, ERRORS, STATUS = ST / "decisions.jsonl", ST / "studies.jsonl", ST / "errors.jsonl", ST / "status.json"
PAUSED, KILLED = ST / "PAUSED", ST / "KILLED"
STUDY_PERIOD = ("2025-01-15", "2026-02-27")
LEARN_PERIOD = ("2026-03-02", "2026-06-15")
EXAM_PERIOD = ("2026-06-16", "2026-09-04")
BLIND_PER_ROUND, STUDIES_PER_ROUND, EXAM_EVERY, EXAM_N = 12, 2, 4, 24
LEARN_ROUNDS = 3           # LEARN reads the graded blind trades of this many past rounds (one round gave it as few as 1 trade)
HORIZON_BARS = 288 * 5
PLAYBOOK_MAX = 2800
RISK_USD = 50.0            # 0.5% of the 10,000 evaluation account, every trade
WORKERS = 8
GRADING = 2
PROMPTS = ST / "prompts.jsonl"
_lock = threading.Lock()
CASTL = Path(os.environ.get("FTMO_BLANK_CASTL_HOME", str(Path.home() / "claude_account_switch")))


def _append(path: Path, rec: dict) -> None:
    ST.mkdir(parents=True, exist_ok=True)
    with _lock, open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def status(**kw) -> None:
    ST.mkdir(parents=True, exist_ok=True)
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps({**kw, "updated": time.time()}))
    os.replace(tmp, STATUS)


def playbook_versions() -> list[int]:
    return sorted(int(p.stem.split("_v")[1]) for p in ST.glob("playbook_v*.md"))


def playbook() -> tuple[int, str]:
    vs = playbook_versions()
    return (vs[-1], (ST / f"playbook_v{vs[-1]}.md").read_text()) if vs else (0, "")


# ---- the walk: what a trade did, on the bars ----------------------------------------------------------------------
def walk_full(bars: list[dict], i: int, sg: int, sd: float, td: float, hs: float, bar_utc, horizon: int = HORIZON_BARS) -> dict:
    """From the next bar's open, half spread each way. R is what the trade made at its first event (the same bar touching both counts as the
    stop), flat before the weekend as the desk is. The walk goes on past a stop to see if the target still came: the idea right, the stop wrong."""
    e = bars[i + 1]["o"] + sg * hs
    rr = td / sd
    first, flat, n_first, n_tp, mae, mfe, last = None, None, None, None, 0.0, 0.0, 0.0
    for n, x in enumerate(bars[i + 1:i + 1 + horizon]):
        worst = (e - (x["l"] - hs)) / sd if sg > 0 else ((x["h"] + hs) - e) / sd
        best = ((x["h"] - hs) - e) / sd if sg > 0 else (e - (x["l"] + hs)) / sd
        last = sg * ((x["c"] - sg * hs) - e) / sd
        if first is None:
            u = bar_utc(x["t"])
            if flat is None and u.weekday() == 4 and u.hour >= 20:
                flat = last
            mae, mfe = max(mae, worst), max(mfe, best)
            if worst >= 1.0:
                first, n_first = "sl", n
            elif best >= rr:
                first, n_first = "tp", n
                break
        else:
            mae = max(mae, worst)
            if best >= rr:
                n_tp = n
                break
    outcome = "tp" if first == "tp" else "sl_then_tp" if n_tp is not None else "sl" if first == "sl" else "open"
    r = flat if flat is not None else rr if first == "tp" else -1.0 if first == "sl" else last
    return {"outcome": outcome, "R": round(r, 3), "rr": round(rr, 2), "bars_first": n_first, "bars_tp": n_tp, "mae": round(mae, 2), "mfe": round(mfe, 2)}


# ---- the model: no ceilings; a real limit moves to the other login, or waits for one -----------------------------------
_meter = {"at": 0.0, "rows": []}
_exhausted: dict = {}
# 2026-11-01: an exhausted account_2 answered "can't use your Claude subscription ... payment is past due" to every call; it was not a limit word, so
# 990 calls in 7 minutes went to it and became ERROR rows while account_1 had room. Now it moves to the other login like any limit.
LIMIT_WORDS = ("usage limit", "rate limit", "limit reached", "hit your limit", "out of extra usage", "resets ", "can't use your claude subscription", "past due",
               "disabled claude subscription access", "not logged in", "please run /login")
ERRORS_TO_PAUSE = 12       # this many failed calls in a row (no success between) writes PAUSED instead of churning rounds of ERROR
_fails = {"run": 0}


def _meters() -> list[tuple[float, str, str]]:
    """(5-hour %, config dir, name), lowest first, read at most every 45 s. A meter that cannot be read counts as 50%."""
    with _lock:
        if time.time() - _meter["at"] < 45 and _meter["rows"]:
            return _meter["rows"]
    if str(CASTL) not in sys.path:
        sys.path.insert(0, str(CASTL))
    from castl_lib import chat as chatmod, usage
    from castl_lib.state import AccountStore
    rows = []
    for name, acc in sorted(AccountStore().all().items()):
        folder = Path(acc["config_dir"]).expanduser()
        until = float(acc.get("exhausted_until") or 0)     # castl's own record of a limit: both meters answered 429 and tied at 50%
        if until > time.time():
            with _lock:
                _exhausted[str(folder)] = max(_exhausted.get(str(folder), 0), until)
        try:
            a = float(usage._get("/api/oauth/usage", usage._token(folder / chatmod.CRED))["five_hour"]["utilization"])
        except Exception:
            a = 50.0
        rows.append((a, str(folder), name))
    rows.sort()
    with _lock:
        _meter.update(at=time.time(), rows=rows)
    return rows


def call(text: str, config_dir: str, effort: str) -> tuple[dict, float]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    env["CLAUDE_CONFIG_DIR"] = config_dir
    wd = ST / "wd"
    wd.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(["claude", "-p", "--output-format", "json", "--model", "sonnet", "--effort", effort, "--tools", "", "--strict-mcp-config"],
                       input=text, capture_output=True, text=True, cwd=wd, timeout=420, env=env)
    raw = (p.stdout or "") + (p.stderr or "")
    try:
        e = json.loads(p.stdout)
    except ValueError:
        raise RuntimeError(("LIMIT " if any(w in raw.lower() for w in LIMIT_WORDS) else "") + raw[-240:])
    t = str(e.get("result") or "")
    if e.get("is_error"):
        raise RuntimeError(("LIMIT " if any(w in t.lower() for w in LIMIT_WORDS) else "") + t[:240])
    m = re.search(r"\{.*\}", t, re.S)
    return (json.loads(m.group(0)) if m else {"action": "?", "raw": t[:200]}), float(e.get("total_cost_usd") or 0)


def ask(text: str, where: str, effort: str = "high", rid: str | None = None) -> tuple[dict | None, float]:
    while not (PAUSED.exists() or KILLED.exists()):
        now = time.time()
        rows = [r for r in _meters() if _exhausted.get(r[1], 0) < now]
        if not rows:
            status(state="waiting", detail="every login is at its limit", round=next_round())
            time.sleep(60)
            continue
        try:
            ans, cost = call(text, rows[0][1], effort)
        except Exception as exc:
            if str(exc).startswith("LIMIT"):
                _exhausted[rows[0][1]] = now + 300
                continue
            _append(ERRORS, {"utc": now, "where": where, "login": rows[0][2], "error": str(exc)[:300]})
            with _lock:
                _fails["run"] += 1
                if _fails["run"] >= ERRORS_TO_PAUSE and not PAUSED.exists():
                    PAUSED.write_text(f"auto-paused: {_fails['run']} failed calls in a row, last on {rows[0][2]}: {str(exc)[:160]}\n")
            return None, 0.0
        with _lock:
            _fails["run"] = 0
        _append(PROMPTS, {"id": rid, "where": where, "utc": now, "login": rows[0][2], "cost": cost, "prompt": text, "answer": ans})
        return ans, cost
    return None, 0.0


# ---- costs and the managed trade: what live would have made ------------------------------------------------------------
_swaps: dict = {}


def swap_usd_per_lot(sym: str, sg: int, last: float, upp: float) -> tuple[float, int]:
    """The broker's swap for one lot, one night (negative = paid), and its triple day (MT5: 0 Sunday .. 3 Wednesday)."""
    if not _swaps:
        try:
            _swaps.update(json.loads((ST / "swaps.json").read_text())["symbols"])
        except (OSError, ValueError):
            _swaps["_none"] = True
    v = _swaps.get(sym)
    if not v:
        return 0.0, 3
    rate = v["long"] if sg > 0 else v["short"]
    if v["mode"] == 1:
        usd = rate * v["point"] * upp
    elif v["mode"] in (5, 6):
        usd = rate / 100 / 360 * last * v["contract"] * upp / max(v["contract"], 1e-9)
    else:
        usd = rate
    return usd, v["triple"]


def rollovers(t0: dt.datetime, t1: dt.datetime, triple: int) -> int:
    """Swap nights charged between two moments, counting the triple day as 3."""
    from zoneinfo import ZoneInfo
    ny = ZoneInfo("America/New_York")
    n, d = 0, t0.astimezone(ny).date()
    while d <= t1.astimezone(ny).date():
        roll = dt.datetime(d.year, d.month, d.day, 17, tzinfo=ny)
        if t0 < roll <= t1 and roll.weekday() < 5:
            n += 3 if (roll.weekday() + 1) % 7 == triple else 1
        d += dt.timedelta(days=1)
    return n


def size(sym: str, stop: float) -> float | None:
    from guard import rules as R
    sp = R.spec(sym)
    raw = RISK_USD / (stop * sp["usd_per_price_unit_per_lot"])
    lots = math.floor(raw / sp["volume_step"] + 1e-9) * sp["volume_step"]
    return round(lots, 6) if lots >= sp["volume_min"] - 1e-12 else None


MANAGE_ANSWER = ("THIS RUN HAS NO TOOLS: nothing to post, journal or consult, and no risk worker to ask for a wider stop. A stop only moves toward profit. Actions take effect at the next "
                 "bar's open.\nAnswer with ONLY one JSON object: {\"action\":\"HOLD|STOP|TARGET|PART|CLOSE\",\"sl\":number or null,\"tp\":number or null,\"fraction\":0-1 or null,"
                 "\"watch\":{\"above\":number or null,\"below\":number or null,\"in_min\":number or null,\"quiet\":true or false,\"quiet_min\":number or null},\"reason\":\"one line\"}")


def managed_trade(rid: str, sym: str, i: int, sg: int, sl0: float, tp0: float, why: str, book: str, hs: float) -> dict | None:
    """The trade bar by bar as the live desk runs it: stop and target at the broker, the manager woken by manage_core's own rules, its actions at the
    next open, flat on Friday, commission and swap charged."""
    import desk_shared as DS
    import manage_core as MC
    import paper as P
    import scout_eval as S
    from guard import rules as R
    bars, sp = S.m_feed(sym), R.spec(sym)
    upp = sp["usd_per_price_unit_per_lot"]
    e = bars[i + 1]["o"] + sg * hs
    if (e - sl0) * sg <= 0 or (tp0 - e) * sg <= 0:
        return None
    lots0 = size(sym, abs(e - sl0))
    if lots0 is None:
        return {"refused": "stop risk over the $ cap at the broker's minimum lot"}
    risk = abs(e - sl0) * upp * lots0
    swap_rate, triple = swap_usd_per_lot(sym, sg, e, upp)
    pos = {"id": rid, "symbol": sym, "side": "buy" if sg > 0 else "sell", "lots": lots0, "entry": round(e, 6), "sl": sl0, "sl0": sl0, "tp": tp0,
           "opened_utc": P.bar_utc(bars[i + 1]["t"]).strftime("%Y-%m-%d %H:%M"), "why": why}
    t_open = P.bar_utc(bars[i + 1]["t"])
    lots, realized, swap, st, pending, events, exit_px, reason, wakes, cost, k, prev = lots0, 0.0, 0.0, None, None, [], None, None, 0, 0.0, i + 1, t_open
    rows = lambda tf, end, n: [{"utc": P.bar_utc(b["t"]).strftime("%Y-%m-%d %H:%M"), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
                               for b in (P.resample(bars[max(0, end + 1 - n * (3 if tf == "M15" else 1) - 6):end + 1], tf, bars[end]["t"], forming_at=bars[end]["t"] + 300))[-n:]]
    for k in range(i + 1, min(i + 1 + HORIZON_BARS, len(bars))):
        x = bars[k]
        u = P.bar_utc(x["t"])
        nights = rollovers(prev, u, triple)
        swap += swap_rate * lots * nights
        prev = u
        if pending:
            px = x["o"] - sg * hs
            if pending[0] == "CLOSE":
                exit_px, reason = px, "manager"
                events.append({"utc": f"{u:%Y-%m-%d %H:%M}", "type": "close", "price": round(px, 6)})
                break
            part = pending[1]
            realized += sg * (px - e) * upp * part
            lots = round(lots - part, 6)
            events.append({"utc": f"{u:%Y-%m-%d %H:%M}", "type": "part", "lots": part, "price": round(px, 6)})
            pending = None
        bid_lo, bid_hi = x["l"] - sg * hs, x["h"] - sg * hs
        if (bid_lo <= pos["sl"]) if sg > 0 else (bid_hi >= pos["sl"]):
            exit_px, reason = pos["sl"], "sl"
            break
        if pos["tp"] and ((bid_hi >= pos["tp"]) if sg > 0 else (bid_lo <= pos["tp"])):
            exit_px, reason = pos["tp"], "tp"
            break
        if u.weekday() == 4 and u.hour >= MC.FRI_FLAT_HOUR_UTC:
            exit_px, reason = x["c"] - sg * hs, "friday_flat"
            break
        now = P.bar_utc(x["t"] + 300)
        minutes = int((now - t_open).total_seconds() // 60)
        n5 = min(300, max(3, minutes // 5 + 3))
        inw = P.in_train_window(x["t"])
        trig, st, info = MC.evaluate(pos, rows("M5", k, n5), rows("M15", k, MC.BOS_LOOKBACK + n5 // 3 + 3), now, in_window=inw, rollover_utc=MC.rollover_utc(now),
                                     friday_flat_utc=MC.friday_flat_utc(now, MC.FRI_FLAT_HOUR_UTC), st=st, m5_hist=None if inw else rows("M5", k, MC.HISTORY_M5), day=P.day_key(x["t"]))
        if not trig:
            continue
        wakes += 1
        S.PACKS.setdefault("blind", (("D1", 5), ("H1", 24), ("M15", 24), ("M5", 12)))
        pk, _ = S.m_packet(sym, k, "blind", 1.0, False)
        text = (f"POSITION MANAGER WAKE: {sym} {pos['side']} -- " + ", ".join(t["code"] for t in trig) + f". Now {now:%Y-%m-%d %H:%M} UTC.\n" + MC.describe(pos, info, trig) + f"\n\n{pk}\n\n"
                + (f"YOUR PLAYBOOK:\n{book}\n\n" if book else "") + DS.MANAGE_TASK + "\n" + MANAGE_ANSWER)
        ans, c = ask(text, "manage", "high", rid)
        cost += c
        ans = ans or {}
        act = str(ans.get("action") or "HOLD").upper()
        ev = {"utc": f"{now:%Y-%m-%d %H:%M}", "type": "wake", "triggers": [t["code"] for t in trig], "action": act, "reason": ans.get("reason"), "price": x["c"]}
        if act == "STOP" and ans.get("sl") is not None:
            ns = float(ans["sl"])
            if (ns - pos["sl"]) * sg > 0 and (x["c"] - ns) * sg > 0:
                pos["sl"] = ns
                ev["sl"] = ns
            else:
                ev["refused"] = "a stop only moves toward profit, on the right side of price"
        elif act == "TARGET" and ans.get("tp") is not None:
            nt = float(ans["tp"])
            if (nt - x["c"]) * sg > 0:
                pos["tp"] = nt
                ev["tp"] = nt
        elif act == "CLOSE":
            pending = ("CLOSE",)
        elif act == "PART":
            step, vmin = sp["volume_step"], sp["volume_min"]
            part = round(math.floor(lots * float(ans.get("fraction") or 0.5) / step + 1e-9) * step, 6)
            if part >= vmin and lots - part >= vmin - 1e-12:
                pending = ("PART", part)
            else:
                ev["refused"] = "both parts must stay above the minimum size"
        w = ans.get("watch") or {}
        if any(w.get(a) not in (None, False) for a in ("above", "below", "in_min", "quiet")):
            st, rep = MC.set_watch(st, now, side=pos["side"], price=x["c"], above=w.get("above"), below=w.get("below"), in_min=w.get("in_min"), quiet=bool(w.get("quiet")),
                                   quiet_min=w.get("quiet_min"), why=str(ans.get("reason") or ""))
            if rep.get("ok"):
                ev["watch"] = rep.get("watch")
        events.append(ev)
    else:
        exit_px, reason = bars[k]["c"] - sg * hs, "horizon"
    if exit_px is None:
        exit_px, reason = bars[k]["c"] - sg * hs, "horizon"
    comm = sp["commission_rt_per_lot"] * lots0
    gross = realized + sg * (exit_px - e) * upp * lots
    net = gross - comm + swap
    return {"entry": round(e, 6), "sl": sl0, "tp": tp0, "lots": lots0, "risk_usd": round(risk, 2), "R": round(net / risk, 3), "R_gross": round(gross / risk, 3), "net_usd": round(net, 2),
            "commission_usd": round(comm, 2), "swap_usd": round(swap, 2), "exit": round(exit_px, 6), "exit_reason": reason, "exit_utc": f"{P.bar_utc(bars[k]['t']):%Y-%m-%d %H:%M}",
            "bars_held": k - i - 1, "wakes": wakes, "events": events, "manage_cost": round(cost, 4), "final_sl": pos["sl"], "final_tp": pos["tp"]}


# ---- STUDY -----------------------------------------------------------------------------------------------------------
def universe() -> list[str]:
    from guard import rules as R
    return [s for s in sorted(R.INSTRUMENTS) if (HOME / "feed" / f"{s}_M5.csv").exists()]


def study(rnd: random.Random, round_no: int) -> dict:
    import paper as P
    import scout_eval as S
    from guard import rules as R
    sym = rnd.choice(universe())
    bars = S.m_feed(sym)
    for _ in range(200):
        i = rnd.randrange(288 * 10, len(bars) - HORIZON_BARS - 800)
        if STUDY_PERIOD[0] <= P.bar_utc(bars[i]["t"]).strftime("%Y-%m-%d") <= STUDY_PERIOD[1]:
            break
    rows = P.resample(bars[i:i + 768], "M15", None)[1:-1]
    table = "\n".join(f"{P.bar_utc(r['t']):%Y-%m-%d %H:%M} {r['o']} {r['h']} {r['l']} {r['c']}" for r in rows)
    text = (f"You are studying a chart the way a trader does at the weekend: every candle is in front of you, including what came next.\n{sym}, M15 bars "
            f"(utc_open open high low close):\n{table}\n\nMark up to 6 trades in this stretch: the ones that worked AND the ones that looked just as good "
            "at the time and failed. For each: the entry bar's label (you enter after that bar closes), side, stop, target at real structure, and what "
            "was visible at or before that bar that made it a trade. Nothing after the entry bar may appear in that reason. Then write up to 3 lessons a "
            "trader could apply with no knowledge of the future.\nAnswer with ONLY one JSON object: {\"trades\":[{\"bar\":\"YYYY-MM-DD HH:MM\",\"side\":\"buy|sell\","
            "\"sl\":number,\"tp\":number,\"worked\":true,\"setup\":\"a few words\",\"seen_before\":\"...\"}],\"lessons\":[{\"if\":\"visible before entry\",\"then\":\"side, stop, target\",\"fails_when\":\"...\"}]}")
    ans, cost = ask(text, "study", "medium", f"study-r{round_no}-{sym}")
    rec = {"round": round_no, "symbol": sym, "from": f"{P.bar_utc(rows[0]['t']):%Y-%m-%d %H:%M}", "to": f"{P.bar_utc(rows[-1]['t']):%Y-%m-%d %H:%M}", "cost": cost, "trades": [], "lessons": []}
    if ans:
        rec["lessons"] = ans.get("lessons") or []
        hs = R.spec(sym)["spread_price_snapshot"] / 2
        by = {f"{P.bar_utc(r['t']):%Y-%m-%d %H:%M}": r for r in rows}
        ts = [b["t"] for b in bars]
        import bisect
        for t in ans.get("trades") or []:
            r = by.get(str(t.get("bar")))
            try:
                sg = 1 if t["side"] == "buy" else -1
                k = bisect.bisect_right(ts, r["t"] + 600)      # the last M5 bar of that M15 bar
                last = bars[k - 1]["c"]
                sd, td = (last - float(t["sl"])) * sg, (float(t["tp"]) - last) * sg
                if sd <= 0 or td <= 0:
                    raise ValueError("levels")
                w = walk_full(bars, k - 1, sg, sd, td, hs, P.bar_utc)
            except (TypeError, KeyError, ValueError):
                rec["trades"].append({**{x: t.get(x) for x in ("bar", "side", "setup")}, "outcome": "invalid"})
                continue
            rec["trades"].append({**{x: t.get(x) for x in ("bar", "side", "setup", "seen_before", "worked")}, **w})
        _append(STUDIES, rec)      # a failed call writes nothing: it is in errors.jsonl, and it must not advance the round
    return rec


# ---- BLIND -----------------------------------------------------------------------------------------------------------
def moments(rnd: random.Random, n: int, period: tuple[str, str], used: set) -> list[tuple[str, int]]:
    import paper as P
    import scout_eval as S
    from zoneinfo import ZoneInfo
    mad = ZoneInfo("Europe/Madrid")
    out: list[tuple[str, int]] = []
    tries = 0
    while len(out) < n and tries < n * 4000:
        tries += 1
        sym = rnd.choice(universe())
        bars = S.m_feed(sym)
        i = rnd.randrange(288 * 30, len(bars) - HORIZON_BARS - 2)
        u = P.bar_utc(bars[i]["t"] + 300)
        loc = u.astimezone(mad)
        key = f"{sym}|{u:%Y-%m-%d}"
        if period[0] <= f"{u:%Y-%m-%d}" <= period[1] and loc.weekday() < 5 and 8 <= loc.hour < 19 and key not in used:
            used.add(key)
            out.append((sym, i))
    return out


def blind_prompt(sym: str, i: int, book: str) -> str:
    import scout_eval as S
    from guard import rules as R
    S.PACKS.setdefault("blind", (("D1", 5), ("H1", 24), ("M15", 24), ("M5", 12)))
    pk, now = S.m_packet(sym, i, "blind", 1.0, False)
    return (f"You trade a prop evaluation; every trade risks the same fixed amount, so what counts is R per trade after costs. A position runs on its stop and "
            f"target, up to five days.\n{sym}. Now: {now:%Y-%m-%d %H:%M} UTC. The last bar has just closed; a market order fills at the next bar's open, about the last close. "
            f"Spread: {R.spec(sym)['spread_price_snapshot']:.5g}.\n\n{pk}\n\n"
            + (f"YOUR PLAYBOOK, from your own studied and graded trades. Where it is silent, trade your own read:\n{book}\n\n" if book else "")
            + "Decide: BUY, SELL or SKIP. A skip is a normal outcome. If you trade, put the stop and the target at real structure.\n"
            'Answer with ONLY one JSON object: {"action":"BUY|SELL|SKIP","sl":number or null,"tp":number or null,"setup":"a few words","reason":"one line"}')


def decide(job: tuple) -> dict:
    import paper as P
    import scout_eval as S
    from guard import rules as R
    phase, round_no, version, arm, sym, i, book = job
    rid = f"r{round_no}-{phase[0]}-{arm[0]}-{sym}-{i}"
    ans, cost = ask(blind_prompt(sym, i, book), f"blind {arm}", "high", rid)
    bars = S.m_feed(sym)
    rec = {"id": rid, "grading": GRADING, "ts": time.time(), "phase": phase, "round": round_no, "version": version, "arm": arm, "symbol": sym, "i": i,
           "utc": f"{P.bar_utc(bars[i]['t'] + 300):%Y-%m-%d %H:%M}", "cost": cost, "action": (ans or {}).get("action", "ERROR"),
           "setup": (ans or {}).get("setup"), "reason": (ans or {}).get("reason")}
    if ans and ans.get("action") in ("BUY", "SELL") and ans.get("sl") and ans.get("tp"):
        sg = 1 if ans["action"] == "BUY" else -1
        last = bars[i]["c"]
        sl, tp = float(ans["sl"]), float(ans["tp"])
        sd, td = (last - sl) * sg, (tp - last) * sg
        if sd > 0 and td > 0:
            hs = R.spec(sym)["spread_price_snapshot"] / 2
            m = managed_trade(rid, sym, i, sg, sl, tp, f"{ans.get('setup')}: {ans.get('reason')}", book, hs)
            if m is None or m.get("refused"):
                rec["outcome"] = "refused" if m else "invalid"
                rec["refused"] = (m or {}).get("refused")
            else:
                w = walk_full(bars, i, sg, sd, td, hs, P.bar_utc)
                mirror = walk_full(bars, i, -sg, sd, td, hs, P.bar_utc)
                rec["mirror_outcome"] = mirror["outcome"]
                risk, upp = m["risk_usd"], R.spec(sym)["usd_per_price_unit_per_lot"]
                t0 = P.bar_utc(bars[i + 1]["t"])
                t1 = P.bar_utc(bars[min(len(bars) - 1, i + 1 + (w["bars_first"] if w["bars_first"] is not None else HORIZON_BARS - 1))]["t"])
                rate, triple = swap_usd_per_lot(sym, sg, last, upp)
                costs = R.spec(sym)["commission_rt_per_lot"] * m["lots"] - rate * m["lots"] * rollovers(t0, t1, triple)
                rec.update({**m, "outcome": w["outcome"], "rr": w["rr"], "mae": w["mae"], "mfe": w["mfe"], "bars_first": w["bars_first"], "bars_tp": w["bars_tp"],
                            "R_hands_off": round(w["R"] - costs / risk, 3), "mirror": round(mirror["R"] - R.spec(sym)["commission_rt_per_lot"] * m["lots"] / risk, 3), "stop_pct": round(sd / last * 100, 3)})
                rec["cost"] = round(cost + m["manage_cost"], 4)
        else:
            rec["outcome"] = "invalid"
    return rec


def blind_batch(phase: str, round_no: int, version: int, book: str, moms: list) -> list[dict]:
    jobs = []
    for sym, i in moms:
        jobs.append((phase, round_no, version, "playbook", sym, i, book))
        jobs.append((phase, round_no, version, "default", sym, i, ""))
    out = []
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for rec in ex.map(decide, jobs):
            if rec["action"] == "ERROR":       # no answer (failed call, or Pause mid-batch): not a decision, and the moment stays unused
                continue
            _append(DECISIONS, rec)
            out.append(rec)
    return out


# ---- LEARN -----------------------------------------------------------------------------------------------------------
def learn(round_no: int, new_studies: list[dict], last_blind: list[dict]) -> int:
    version, book = playbook()
    checked = []
    for s in new_studies:
        for t in s["trades"]:
            if t.get("outcome") != "invalid":
                checked.append(f"{s['symbol']} {t.get('side')} {t.get('setup')}: claimed {'win' if t.get('worked') else 'loss'}, the bars say {t.get('outcome')} {t.get('R'):+.2f}R "
                               f"(rr {t.get('rr')}, went {t.get('mae')}R against) | visible before: {str(t.get('seen_before'))[:170]}")
    lessons = [f"IF {x.get('if')} THEN {x.get('then')} FAILS WHEN {x.get('fails_when')}" for s in new_studies for x in s["lessons"]]
    graded = [f"{r['symbol']} {r['action']} [{r['arm']}] {r.get('setup')}: {r['outcome']}, managed {r['R']:+.2f}R net, hands-off {r['R_hands_off']:+.2f}R net, exit {r['exit_reason']}, rr {r['rr']}, "
              f"went {r['mae']}R against, reached {r['mfe']}R | other side {r.get('mirror_outcome') or ''} {r['mirror']:+.2f}R" for r in last_blind if "R" in r]
    # 2026-11-01: skips were never graded. The paired default arm is the ruler with real levels: a moment the playbook skipped and the default traded.
    key = lambda r: (r["round"], r["symbol"], r["i"])
    took = {key(r): r for r in last_blind if r["arm"] == "default" and "R" in r}
    missed = [f"{t['symbol']} {t['action']} {t.get('setup')}: {t['outcome']}, managed {t['R']:+.2f}R net, hands-off {t['R_hands_off']:+.2f}R net, rr {t['rr']}"
              for r in last_blind if r["arm"] == "playbook" and r["action"] == "SKIP" and (t := took.get(key(r)))]
    s = summary()
    arms = {a: s["phases"].get("learn", {}).get(a) for a in ("playbook", "default")}
    text = ("You keep a trading playbook that you read when you decide at a chart with no knowledge of the future. Rewrite it from the evidence below; code graded all of it.\n\n"
            f"THE PLAYBOOK NOW (v{version}):\n{book or '(empty)'}\n\nSTUDIED CHARTS, every trade checked on the bars:\n" + ("\n".join(checked) or "(none)")
            + "\n\nLESSONS YOU DREW FROM THEM:\n" + ("\n".join(lessons) or "(none)")
            + f"\n\nBLIND TRADES, LAST {LEARN_ROUNDS} ROUNDS (sl_then_tp = stopped, then the target came within five days. 'other side' is the opposite trade at the same "
            "moment with the same distances: when it also got there, a stop that was later 'right' says nothing about the idea):\n" + ("\n".join(graded) or "(none yet)")
            + "\n\nMOMENTS YOUR PLAYBOOK SKIPPED AND THE NO-PLAYBOOK TRADER TOOK, graded the same way (what the skip cost or saved):\n" + ("\n".join(missed) or "(none)")
            + f"\n\nTHE RECORD SO FAR, with the playbook and with no playbook on the same moments: {json.dumps(arms)}\nBY SETUP: {json.dumps(s['setups'][:12])}\n\n"
            f"Rules for the playbook. At most {PLAYBOOK_MAX} characters. Say what to TAKE: how to recognise it from candles before the entry, the side, where the stop goes, where the "
            "target goes. A rule that says not to trade needs at least 8 graded trades behind it, and say the count. Never tell yourself to trade less: a playbook that only "
            "shrinks the number of trades has failed. If the record shows the playbook losing to no playbook, change it. No dates and no specific prices.\n"
            'Answer with ONLY one JSON object: {"playbook":"...","changed":"one line"}')
    ans, cost = ask(text, "learn", "medium", f"learn-r{round_no}")
    new = str((ans or {}).get("playbook") or "").strip()
    if not new or len(new) > PLAYBOOK_MAX * 1.5:      # was 1.2: every learn from 12:30 on came back at 3,435-3,558 chars and was refused, so v5 never changed
        if ans is not None:      # a failed call is already in errors.jsonl
            _append(ERRORS, {"utc": time.time(), "where": "learn", "error": f"playbook refused, {len(new)} chars"})
        return version
    version += 1
    (ST / f"playbook_v{version}.md").write_text(new)
    _append(ST / "versions.jsonl", {"version": version, "round": round_no, "changed": (ans or {}).get("changed"), "chars": len(new), "cost": cost, "utc": time.time()})
    return version


# ---- what the dashboard reads ------------------------------------------------------------------------------------------
def _stat(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    m = sum(xs) / len(xs)
    sd = (sum((x - m) ** 2 for x in xs) / max(1, len(xs) - 1)) ** 0.5
    return {"n": len(xs), "mean": round(m, 3), "se": round(sd / len(xs) ** 0.5, 3), "win": round(sum(x > 0 for x in xs) / len(xs), 3), "sum": round(sum(xs), 2)}


def summary() -> dict:
    rows = [r for r in _rows(DECISIONS) if r.get("grading") == GRADING and r.get("action") != "ERROR"]
    studies = _rows(STUDIES)
    out: dict = {"phases": {}, "setups": [], "versions": [], "stop": {}, "cost": round(sum(r.get("cost") or 0 for r in rows + studies) + sum(v.get("cost") or 0 for v in _rows(ST / "versions.jsonl")), 2),
                 "decisions": len(rows)}
    for ph in ("learn", "exam"):
        pr = [r for r in rows if r.get("phase") == ph]
        d = {}
        for arm in ("playbook", "default"):
            ar = [r for r in pr if r["arm"] == arm]
            t = [r for r in ar if "R" in r]
            ou: dict = {}
            for r in t:
                ou[r["outcome"]] = ou.get(r["outcome"], 0) + 1
            d[arm] = {"moments": len(ar), "trades": len(t), "take": round(len(t) / len(ar), 3) if ar else None, **_stat([r["R"] for r in t]),
                      "side_skill": _stat([r["R_hands_off"] - r["mirror"] for r in t]).get("mean"), "hands_off": _stat([r["R_hands_off"] for r in t]).get("mean"),
                      "manager_value": _stat([r["R"] - r["R_hands_off"] for r in t]).get("mean"), "wakes": sum(r.get("wakes", 0) for r in t),
                      "swap_usd": round(sum(r.get("swap_usd", 0) for r in t), 2), "commission_usd": round(sum(r.get("commission_usd", 0) for r in t), 2), "outcomes": ou}
        key = lambda r: (r["round"], r["symbol"], r["i"])
        a, b = {key(r): r.get("R", 0.0) for r in pr if r["arm"] == "playbook"}, {key(r): r.get("R", 0.0) for r in pr if r["arm"] == "default"}
        d["paired_R_per_moment"] = _stat([a[k] - b[k] for k in a if k in b])
        out["phases"][ph] = d
    vs: dict = {}
    for r in [x for x in rows if x["arm"] == "playbook" and "R" in x]:
        vs.setdefault(r["version"], []).append(r["R"])
    meta = {v["version"]: v for v in _rows(ST / "versions.jsonl")}
    out["versions"] = [{"version": k, **_stat(v), "changed": meta.get(k, {}).get("changed")} for k, v in sorted(vs.items())]
    su: dict = {}
    for r in [x for x in rows if "R" in x]:
        su.setdefault(str(r.get("setup") or "?").lower()[:40], []).append(r["R"])
    out["setups"] = sorted(({"setup": k, **_stat(v)} for k, v in su.items()), key=lambda x: -x["n"])[:25]
    sl = [r for r in rows if r.get("outcome") in ("sl", "sl_then_tp")]
    swept = [r for r in sl if r["outcome"] == "sl_then_tp"]
    med = sorted(r["mae"] for r in swept)
    out["stop"] = {"stopped": len(sl), "then_target": len(swept), "share": round(len(swept) / len(sl), 3) if sl else None, "median_R_against": med[len(med) // 2] if med else None}
    # the ruler for "the idea was right, the stop was not": the opposite trade, same moment and distances. Getting there as often = the window, not the idea.
    ms = [r for r in rows if r.get("mirror_outcome") in ("sl", "sl_then_tp")]
    out["stop"].update(mirror_stopped=len(ms), mirror_then_target=sum(1 for r in ms if r["mirror_outcome"] == "sl_then_tp"),
                       mirror_share=round(sum(1 for r in ms if r["mirror_outcome"] == "sl_then_tp") / len(ms), 3) if ms else None)
    st = [t for s in studies for t in s["trades"]]
    ok = [t for t in st if t.get("outcome") not in (None, "invalid")]
    out["study"] = {"charts": len(studies), "marked": len(st), "checked": len(ok), "claim_true": sum(1 for t in ok if bool(t.get("worked")) == (t["outcome"] == "tp")), "wins_on_bars": sum(1 for t in ok if t["outcome"] == "tp")}
    out["recent"] = [{k: r.get(k) for k in ("id", "phase", "version", "arm", "utc", "symbol", "action", "setup", "outcome", "exit_reason", "R", "R_hands_off", "rr", "mae", "wakes", "reason")} for r in rows[-40:]][::-1]
    ts = [r["ts"] for r in rows if r.get("ts")]
    out["speed"] = {"decisions_per_hour": round(len(ts) / max((max(ts) - min(ts)) / 3600, 1e-6), 1) if len(ts) > 1 else None, "cost_per_decision": round(out["cost"] / max(1, len(rows)), 3),
                    "trades": sum(1 for r in rows if "R" in r), "cost_per_trade": round(out["cost"] / max(1, sum(1 for r in rows if "R" in r)), 3)}
    out["recent_lessons"] = [x for s in studies[-3:] for x in s["lessons"]][:8]
    out["curve"] = {a: [r["R"] for r in rows if r["arm"] == a and "R" in r] for a in ("playbook", "default")}
    v, book = playbook()
    out["playbook_version"], out["playbook"] = v, book
    out["errors"] = _rows(ERRORS)[-5:]
    out["status"] = json.loads(STATUS.read_text()) if STATUS.exists() else {}
    out["paused"], out["killed"] = PAUSED.exists(), KILLED.exists()
    return out


# ---- the loop ----------------------------------------------------------------------------------------------------------
def one_round(round_no: int, say) -> None:
    rnd = random.Random(f"bc2-{round_no}")
    used = {f"{r['symbol']}|{r['utc'][:10]}" for r in _rows(DECISIONS)}
    say("study")
    with cf.ThreadPoolExecutor(STUDIES_PER_ROUND) as ex:
        studies = list(ex.map(lambda r: study(r, round_no), [random.Random(f"{round_no}-{k}") for k in range(STUDIES_PER_ROUND)]))
    say("learn")
    prev = [r for r in _rows(DECISIONS) if r.get("phase") == "learn" and round_no - LEARN_ROUNDS <= r.get("round", 0) < round_no and r.get("grading") == GRADING
            and r.get("action") != "ERROR"]
    version = learn(round_no, studies, prev)
    _, book = playbook()
    say("blind")
    blind_batch("learn", round_no, version, book, moments(rnd, BLIND_PER_ROUND, LEARN_PERIOD, used))
    if round_no % EXAM_EVERY == 0:
        say("exam")
        blind_batch("exam", round_no, version, book, moments(rnd, EXAM_N, EXAM_PERIOD, used))


def next_round() -> int:
    rs = [r.get("round", 0) for r in _rows(DECISIONS) + _rows(STUDIES) + _rows(ST / "versions.jsonl")]
    return (max(rs) + 1) if rs else 1


def run(max_rounds: int | None = None) -> int:
    done = 0
    while not PAUSED.exists() and not KILLED.exists():
        round_no = next_round()
        one_round(round_no, lambda ph: status(state="running", round=round_no, phase=ph, detail=""))
        done += 1
        if max_rounds and done >= max_rounds:
            break
    status(state="stopped", round=next_round(), detail="killed" if KILLED.exists() else "paused" if PAUSED.exists() else "finished")
    return 0


def trade_detail(rid: str) -> dict:
    """One saved trade for the dashboard's replay: its record, every prompt and answer, and the bars around it."""
    import paper as P
    import scout_eval as S
    rec = next((r for r in _rows(DECISIONS) if r.get("id") == rid), None)
    if rec is None:
        return {"error": "no such id"}
    bars = S.m_feed(rec["symbol"])
    i, held = rec["i"], rec.get("bars_held") or 0
    b = bars[max(0, i - 48):min(len(bars), i + held + 49)]
    return {**rec, "entry_bar": min(48, i), "bars": [[P.bar_utc(x["t"]).strftime("%m-%d %H:%M"), x["o"], x["h"], x["l"], x["c"]] for x in b],
            "calls": [{k: c.get(k) for k in ("where", "cost", "answer")} for c in _rows(PROMPTS) if c.get("id") == rid]}


def replay() -> dict:
    """Every saved trade again, no model call: stored levels, walked hands-off, stored managed result beside it. Proves the saved record is enough."""
    import paper as P
    import scout_eval as S
    from guard import rules as R
    out = []
    for r in _rows(DECISIONS):
        if r.get("grading") != GRADING or "R" not in r:
            continue
        sg = 1 if r["action"] == "BUY" else -1
        bars, last = S.m_feed(r["symbol"]), S.m_feed(r["symbol"])[r["i"]]["c"]
        w = walk_full(bars, r["i"], sg, (last - r["sl"]) * sg, (r["tp"] - last) * sg, R.spec(r["symbol"])["spread_price_snapshot"] / 2, P.bar_utc)
        out.append({"id": r["id"], "stored_outcome": r["outcome"], "replayed_outcome": w["outcome"], "managed_R": r["R"]})
    return {"trades": len(out), "same_outcome": sum(1 for x in out if x["stored_outcome"] == x["replayed_outcome"]), "rows": out[:5]}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "run":
        sys.exit(run())
    if cmd == "once":
        sys.exit(run(1))
    if cmd == "show":
        print(json.dumps({k: v for k, v in trade_detail(sys.argv[2]).items() if k != "bars"}, indent=1)[:6000])
        sys.exit(0)
    if cmd == "replay":
        print(json.dumps(replay(), indent=1))
        sys.exit(0)
    print(json.dumps({k: v for k, v in summary().items() if k not in ("recent", "curve")}, indent=1)[:3000])
