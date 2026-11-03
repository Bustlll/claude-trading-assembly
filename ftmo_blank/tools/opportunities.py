"""OPPORTUNITIES: every move the desk looked at and did NOT trade, walked forward by code (2026-10-18).

Operator: "make another worker for tracking the rejections of trades and what would have been its result after
the forward walk, and inject the learnings from the non-traded rejections, so that it auto-configures itself".
Before this the desk learned only from closed trades; a desk that rejects almost everything learns almost
nothing, and never finds out whether its filters reject losers or winners.

The tracking is code (exact, free, the same on both desks); the learning is a worker:
  1. grade_day(): at each day close, every mover the move screen flagged that day (setup_candidate) and that was
     not traded within TAKEN_WITHIN_H is walked forward HORIZON_H on M5 bars with a fixed yardstick -- a trade
     WITH the move and a FADE against it, stop YARD_STOP_ATR x ATR(M15), target YARD_RR x the stop -- plus how far
     the move ran on and against (in ATR). The scout's verdict and reason codes come from its board post (VERDICT
     lines, else the codes it wrote). Met-and-passed alerts are graded on their own plan (tools/pass_grade.py).
     Rows go to state/opportunities.jsonl (training) / state/live_opportunities.jsonl (live).
  2. cards(): calibration cards from those rows -- per symbol and per reason code -- that tools/pathways.py adds
     to the lessons library, ranked first for their symbol: every scout, trader and decider reads its symbol's
     calibration automatically (the injection).
  3. calibration_text(): the whole table for the periodic calibration review (a review worker every
     CALIBRATE_EVERY_DAYS trading days), which turns the numbers into lessons: which filters cost money, which
     save it, what a good setup looks like on this symbol by the record.
The yardstick is a ruler, not a strategy: it tells whether a rejected move went on or turned, beyond noise.
Training rows are dated on the replay's clock and only rows up to its "now" are ever shown (no future).
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

HORIZON_H = 4
TAKEN_WITHIN_H = 2
YARD_STOP_ATR = 1.5
YARD_RR = 2.0
VERDICT_WINDOW_MIN = 20
CALIBRATE_EVERY_DAYS = 5
MIN_FOR_CARD = 4
FETCH_M5, FETCH_M15 = 500, 200   # bars a day close reads per symbol (live_market.bars serves at most 500)
VERDICTS = ("NOW", "ALERT", "WATCH", "DISMISS", "PASS", "SKIP")


def _t(s) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(str(s)[:16], "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def path(desk: str, home) -> Path:
    return Path(home) / "state" / ("opportunities.jsonl" if desk == "training" else "live_opportunities.jsonl")


def load(desk: str, home, upto: dt.datetime | None = None) -> list[dict]:
    p = path(desk, home)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if upto is None or ((_t(r.get("utc")) or upto) <= upto):
            rows.append(r)
    return rows


# ---- the verdict a worker gave ------------------------------------------------------------------
def verdict_of(symbol: str, at: dt.datetime, posts: list[dict]) -> dict:
    """The first board post about `symbol` within VERDICT_WINDOW_MIN after `at`: its VERDICT line for the symbol if
    it wrote one (VERDICT SYM WORD codes | reason), else the verdict word and codes found in its text."""
    base = symbol.split(".")[0].upper()
    for p in posts:
        tp = _t(p.get("utc"))
        if tp is None or not (at <= tp <= at + dt.timedelta(minutes=VERDICT_WINDOW_MIN)):
            continue
        text = str(p.get("text", ""))
        if str(p.get("symbol", "")).upper().split(".")[0] != base and base not in text.upper():
            continue
        m = re.search(rf"VERDICT\s+{re.escape(base)}\S*\s+({'|'.join(VERDICTS)})\b([^\n|]*)\|?([^\n]*)", text, re.I)
        if m:
            return {"verdict": m.group(1).upper(), "codes": sorted(set(re.findall(r"\b([SA][1-5])\b", m.group(2)))),
                    "reason": (m.group(2) + " " + m.group(3)).strip()[:200], "by": p.get("worker")}
        words = [w for w in VERDICTS if re.search(rf"\b{w}\b", text)]
        return {"verdict": words[0] if words else "?", "codes": sorted(set(re.findall(r"\b([SA][1-5])\b", text))),
                "reason": text[:200], "by": p.get("worker")}
    return {"verdict": "?", "codes": [], "reason": "no board post found", "by": None}


# ---- the forward walk ---------------------------------------------------------------------------
def _yard(side: str, entry: float, stop_dist: float, after: list[dict]) -> str:
    tp_dist = YARD_RR * stop_dist
    for b in after:
        adv = (entry - b["l"]) if side == "buy" else (b["h"] - entry)
        fav = (b["h"] - entry) if side == "buy" else (entry - b["l"])
        if adv >= stop_dist:
            return "stop"
        if fav >= tp_dist:
            return "target"
    return "open"


def walk(at_utc: str, direction: int, m5: list[dict], m15: list[dict]) -> dict | None:
    """The yardstick on the bars after the bar opening at at_utc (rows like bars_view: utc, o, h, l, c)."""
    t0 = str(at_utc)[:16]
    start = _t(t0)
    bar = next((b for b in m5 if str(b["utc"])[:16] == t0), None)
    if bar is None or start is None:
        return None
    before = [b for b in m15 if str(b["utc"])[:16] < t0][-14:]
    atr = sum(b["h"] - b["l"] for b in before) / len(before) if before else 0.0
    if atr <= 0:
        return None
    t_end = start + dt.timedelta(hours=HORIZON_H)
    after = [b for b in m5 if str(b["utc"])[:16] > t0 and (_t(b["utc"]) or t_end) <= t_end]
    if not after:
        return None
    entry, stop = bar["c"], YARD_STOP_ATR * atr
    up = direction > 0
    run_on = max(((b["h"] - entry) if up else (entry - b["l"])) for b in after) / atr
    run_back = max(((entry - b["l"]) if up else (b["h"] - entry)) for b in after) / atr
    return {"entry": entry, "atr": round(atr, 6), "with_move": _yard("buy" if up else "sell", entry, stop, after),
            "fade": _yard("sell" if up else "buy", entry, stop, after), "ran_on_atr": round(run_on, 2),
            "ran_back_atr": round(run_back, 2),
            "complete": (_t(after[-1]["utc"]) or start) >= t_end - dt.timedelta(minutes=5)}


def grade_day(rows: list[dict], posts: list[dict], day: str, day_of, fetch, desk: str) -> list[dict]:
    """The day's flagged movers that were not traded, walked forward. fetch(symbol, tf, n) -> bars_view rows."""
    out, cache = [], {}
    for r in rows:
        if r.get("type") != "setup_candidate" or day_of(r) != day:
            continue
        at = _t(r.get("utc"))
        if at is None:
            continue
        for c in r.get("candidates") or []:
            sym = c["symbol"]
            taken = any(o.get("type") == "order_placed" and o.get("symbol") == sym
                        and (to := _t(o.get("utc") or o.get("logged_utc"))) and at <= to <= at + dt.timedelta(hours=TAKEN_WITHIN_H)
                        for o in rows)
            if taken:
                continue
            if sym not in cache:
                cache[sym] = (fetch(sym, "M5", FETCH_M5), fetch(sym, "M15", FETCH_M15))
            w = walk(r["utc"], 1 if c.get("pct_move", 0) > 0 else -1, *cache[sym])
            if w is None:
                continue
            out.append({"desk": desk, "day": day, "utc": str(r["utc"])[:16], "symbol": sym, "kind": "mover",
                        "pct_move": c.get("pct_move"), **verdict_of(sym, at, posts), **w})
    return out


def record(rows: list[dict], desk: str, home) -> int:
    """Append newly graded rows (one per desk/utc/symbol/kind)."""
    p = path(desk, home)
    have = {(r.get("utc"), r.get("symbol"), r.get("kind")) for r in load(desk, home)}
    new = [r for r in rows if (r["utc"], r["symbol"], r["kind"]) not in have]
    if new:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            for r in new:
                f.write(json.dumps(r) + "\n")
    return len(new)


def pass_rows(graded_passes: list[dict], desk: str, day: str) -> list[dict]:
    """tools/pass_grade.py results as opportunity rows (kind pass)."""
    out = []
    for g in graded_passes:
        res = str(g.get("result", ""))
        if res.startswith(("step 1", "not graded")):
            continue
        outcome = "target" if res.startswith("TARGET") else "stop" if res.startswith("STOP") else "open"
        out.append({"desk": desk, "day": day, "utc": str(g.get("at") or "")[:16], "symbol": g.get("symbol"),
                    "kind": "pass", "verdict": "PASS", "codes": sorted(set(re.findall(r"\b([SA][1-5])\b",
                                                                                      str(g.get("why") or "")))),
                    "reason": str(g.get("decision_note") or "")[:200], "plan_result": outcome, "rr": g.get("rr")})
    return out


# ---- what the record says ------------------------------------------------------------------------
def _r(outcome: str) -> float:
    return YARD_RR if outcome == "target" else -1.0 if outcome == "stop" else 0.0


def _stats(rows: list[dict], key: str) -> str:
    done = [r for r in rows if r.get(key) in ("target", "stop", "open")]
    if not done:
        return "-"
    t = sum(r[key] == "target" for r in done)
    s = sum(r[key] == "stop" for r in done)
    mean = sum(_r(r[key]) for r in done) / len(done)
    return f"target first {t}/{len(done)}, stop first {s}, mean {mean:+.2f}R"


def summarize(rows: list[dict], label: str) -> str:
    movers = [r for r in rows if r.get("kind") == "mover"]
    passes = [r for r in rows if r.get("kind") == "pass"]
    lines = [f"{label}: {len(movers)} mover(s) looked at and not traded, {len(passes)} graded alert pass(es)."]
    if movers:
        lines.append(f"  a {YARD_RR:g}R trade WITH the move (stop {YARD_STOP_ATR:g} ATR M15): " + _stats(movers, "with_move"))
        lines.append(f"  a {YARD_RR:g}R FADE of the move: " + _stats(movers, "fade"))
        groups: dict[str, list] = {}
        for r in movers:
            for k in ([f"{r.get('verdict', '?')} {c}" for c in r.get("codes") or []] or [r.get("verdict", "?")]):
                groups.setdefault(k, []).append(r)
        for k, rs in sorted(groups.items(), key=lambda x: -len(x[1]))[:8]:
            lines.append(f"  - {k} ({len(rs)}): with the move {_stats(rs, 'with_move')}; fade {_stats(rs, 'fade')}")
    if passes:
        lines.append("  alert passes on their own plan: " + _stats([{**r, "with_move": r.get("plan_result")}
                                                                     for r in passes], "with_move").replace("mean", "mean (plan RR)"))
    return "\n".join(lines)


def cards(desk: str, home, upto: dt.datetime | None = None) -> list[tuple[str, str, list[str], str]]:
    """(id, symbol or None, codes, text) calibration cards for the lessons library: one per symbol with enough
    rows, one desk-wide per reason code."""
    rows = load(desk, home, upto)
    if not rows:
        return []
    out = []
    last = max(str(r.get("utc")) for r in rows)
    by_sym: dict[str, list] = {}
    for r in rows:
        by_sym.setdefault(r["symbol"], []).append(r)
    for sym, rs in by_sym.items():
        if len(rs) >= MIN_FOR_CARD:
            codes = sorted({c for r in rs for c in r.get("codes") or []})
            out.append((f"C:{sym}", sym, codes, "CALIBRATION (code-graded record of what this desk did NOT trade, "
                        f"{rs[0]['utc'][:10]}..{rs[-1]['utc'][:10]}; facts, not advice):\n" + summarize(rs, sym), last))
    by_code: dict[str, list] = {}
    for r in rows:
        for c in r.get("codes") or []:
            by_code.setdefault(c, []).append(r)
    for code, rs in by_code.items():
        if len(rs) >= MIN_FOR_CARD:
            out.append((f"C:{code}", None, [code], f"CALIBRATION of reason {code} across every symbol (code-graded "
                        "record of rejections):\n" + summarize(rs, f"rejections citing {code}"), last))
    return out


def calibration_text(desk: str, home, upto: dt.datetime | None = None) -> str:
    rows = load(desk, home, upto)
    if not rows:
        return ""
    syms = sorted({r["symbol"] for r in rows}, key=lambda s: -sum(r["symbol"] == s for r in rows))
    return "\n".join([summarize(rows, "THE WHOLE DESK")] + [summarize([r for r in rows if r["symbol"] == s], s)
                                                            for s in syms[:10]])


# ---- the trades that WERE taken: what price did after they closed (2026-10-20) ---------------------------------------
# Operator, 10-20: "why didn't the day close acknowledge the later win after the stop-out? ... it should, and extract
# knowledge from it". The trade review runs minutes after a close, the day review only saw the rejections: no worker
# ever saw that USOIL ran through its old target 2.5 h after the stop. Now every trade closed that day is walked forward
# from its close by code, and the day review is told to turn a swept stop into a stop-placement lesson.
def after_path(desk: str, home) -> Path:
    return Path(home) / "state" / ("after_close.jsonl" if desk == "training" else "live_after_close.jsonl")


def closed_after(rows: list[dict], day: str, day_of, fetch, desk: str) -> list[dict]:
    out = []
    for r in rows:
        if r.get("type") != "trade_closed" or day_of(r) != day:
            continue
        try:
            entry, side = float(r["entry"]), r["side"]
            sl0 = float(r.get("initial_sl") or r.get("sl"))
        except (KeyError, TypeError, ValueError):
            continue
        risk = abs(entry - sl0)
        t_close = _t(str(r.get("closed_utc") or "")[:16])
        if risk <= 0 or t_close is None:
            continue
        bars = [b for b in fetch(r["symbol"], "M5", FETCH_M5) if "error" not in b and (_t(b["utc"]) or t_close) > t_close]
        if not bars:
            continue
        buy = side == "buy"
        tp = r.get("tp")
        best = max(b["h"] for b in bars) if buy else min(b["l"] for b in bars)
        hit_tp = next((b["utc"] for b in bars if tp and ((b["h"] >= float(tp)) if buy else (b["l"] <= float(tp)))), None)
        hit_1r = next((b["utc"] for b in bars if ((b["h"] - entry) if buy else (entry - b["l"])) >= risk), None)
        out.append({"desk": desk, "day": day, "utc": str(r.get("closed_utc"))[:16], "symbol": r["symbol"], "side": side,
                    "kind": "after_close", "reason": r.get("reason"), "r": r.get("r_net", r.get("r_price")),
                    "entry": entry, "stop": sl0, "target": tp, "best_after_r": round(((best - entry) if buy else
                                                                                     (entry - best)) / risk, 2),
                    "target_after_utc": hit_tp, "plus_1r_after_utc": hit_1r,
                    "hours_watched": round(((_t(bars[-1]["utc"]) or t_close) - t_close).total_seconds() / 3600 + 1 / 12, 1),
                    "swept": bool(r.get("reason") == "sl" and (hit_tp or hit_1r))})
    return out


def after_text(rows: list[dict]) -> str:
    lines = ["TODAY'S CLOSED TRADES -- WHAT PRICE DID AFTER THEY CLOSED (code; closed bars from the close up to now):"]
    for a in rows:
        head = (f"  - {a['symbol']} {a['side']} closed {a['utc']} by {a['reason']} ({a['r']}R; stop {a['stop']}, target "
                f"{a['target']}): watched {a['hours_watched']} h after;")
        if a["reason"] == "sl" and a["target_after_utc"]:
            lines.append(f"{head} AFTER THE STOP PRICE REACHED THE TARGET at {a['target_after_utc']} UTC (best "
                         f"{a['best_after_r']:+.2f}R from the entry) -- the stop was swept, the idea was right")
        elif a["reason"] == "sl" and a["plus_1r_after_utc"]:
            lines.append(f"{head} after the stop price reached +1R from the entry at {a['plus_1r_after_utc']} UTC (best "
                         f"{a['best_after_r']:+.2f}R) -- swept")
        elif a["reason"] == "tp":
            lines.append(f"{head} after the target price went on to {a['best_after_r']:+.2f}R from the entry")
        else:
            lines.append(f"{head} best after the close {a['best_after_r']:+.2f}R from the entry")
    return "\n".join(lines)


CLOSED_TASK = """THE TRADES TAKEN, AFTER THEY CLOSED (above): for every stopped trade that afterwards reached its target (or
+1R), the stop was SWEPT while the idea was right -- say what stop would have survived (beyond the plan's close-based
invalidation, past the wick reach the chart consult or the vault measures) and post it as a lesson for that symbol
(topic lesson). For a target that kept running far past it, say what the trade left on the table and whether a trail
or a later target would have held it."""


def day_review(desk: str, rows: list[dict], posts: list[dict], day: str, day_of, fetch, home,
               note_types=("note", "journal")) -> tuple[int, str]:
    """At a day close: grade the day's passes and rejected movers, record them (the calibration cards and the
    calibration review read the file), and return (how many were graded, the day review's text block)."""
    import pass_grade as PG
    gp = PG.graded(rows, day, day_of, lambda s: fetch(s, "M5", FETCH_M5), note_types)
    movers = grade_day(rows, posts, day, day_of, fetch, desk)
    record(movers + pass_rows(gp, desk, day), desk, home)
    real = [g for g in gp if not str(g["result"]).startswith("step 1")]
    after = closed_after(rows, day, day_of, fetch, desk)
    if after:
        ap = after_path(desk, home)
        ap.parent.mkdir(parents=True, exist_ok=True)
        with open(ap, "a") as f:
            for a in after:
                f.write(json.dumps(a) + "\n")
    parts = [after_text(after) + "\n" + CLOSED_TASK] if after else []
    if gp:
        parts.append(PG.text(gp) + ("\n" + PG.TASK if real else ""))
    if movers:
        parts.append(summarize(movers, f"TODAY'S REJECTED MOVERS ({day}, code-graded, {HORIZON_H}h after each flag)")
                     + "\n" + "\n".join(f"  - {m['utc']} {m['symbol']} {m['pct_move']:+.2f}% -> verdict {m['verdict']} "
                                         f"{' '.join(m['codes'])}: with the move {m['with_move']}, fade {m['fade']}, ran on "
                                         f"{m['ran_on_atr']} ATR / back {m['ran_back_atr']} ATR" for m in movers[:12])
                     + "\n" + DAY_TASK)
    return len(real) + len(movers) + len(after), "\n\n".join(parts)


DAY_TASK = """THE DAY'S REJECTED MOVERS above are graded by code with a fixed BLIND ruler (a trade with the move and a fade,
both 2R on a 1.5-ATR stop). A blind ruler loses on almost any mover, so "the blind trade would have lost" never proves a
skip was right. The question is the other one: which rejected movers ran far (ran on >= 3 ATR) and what entry -- the
setup code, the level, the trigger -- would have taken them. Post that entry, if any, as the lesson (topic lesson,
--symbol S): a lesson says what to TAKE next time, not that skipping was correct."""

CALIBRATE_TASK = """CALIBRATION REVIEW. Above is this desk's code-graded record of what it did NOT trade: every flagged
mover walked forward with a fixed BLIND ruler (a 2R trade with the move and a 2R fade, stop 1.5 x ATR M15), plus the
alert passes graded on their own plans. A blind ruler loses on almost every mover in both directions -- that is a fact
about blind entries, not evidence that the desk's skips were right.
For each symbol with enough rows, find what to TAKE:
  1. the rejected movers that ran on (or turned) far, and the entry that would have caught them: setup code, level,
     trigger, stop past the sweeps, hour;
  2. the alert plans that were passed and then paid on their own plan -- each one is a decline that cost money;
  3. any skip reason (A1-A5, R:R, 'marginal pierce', sessions) whose rejections ran on more often than the blind ruler's
     baseline: DROP or narrow it, with the numbers.
Post ONE lesson per symbol that says which entry to take next time, its text starting "TAKE SYM:" (board post --from
review --topic lesson --symbol S --text "TAKE SYM: ..."). "Keep every skip" is not a lesson. If a rule should change in code or the checklist, write a paper."""
