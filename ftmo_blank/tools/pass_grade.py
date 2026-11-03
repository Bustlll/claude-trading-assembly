"""PASS GRADING (2026-10-18): a plan that was met and NOT taken is graded by code against what price did next.

Why: from 2025-05-27 on the training desk, 17 alerts were met and 16 of them were declined by the woken trader
("pierce only 1.3 pts", "R:R 0.55", "inside the US open") -- each with a reason, none ever checked. Reviews and
lessons only follow a CLOSED TRADE, so two simulated weeks of decisions taught the desk nothing. Here every pass
of the day is replayed on the bars that followed, with the plan's OWN stop and target from its --why (the
vocabulary's grammar: "S2 SELL @3290 SL 3302 TP 3266 ..."), and the day's review grades whether the reason for
passing was right. Same code on both desks (training: the replay's bars up to its own clock -- never later;
live: the broker's closed bars).

A plan whose why states no stop or no target as a price is named, not graded (the review sees why). A plan that
is step 1 of two ("T-CLOSE ... then RETEST") is not a pass: it was never an entry.
"""
from __future__ import annotations

import datetime as dt
import re

ORDER_WINDOW_MIN = 30       # an order on the symbol within this long after the alert = taken, not passed
HORIZON_H = 24              # a pass is judged on the day after it, no further (the plan's own life)
PLAUSIBLE = 0.10            # a stop/target more than 10% from the entry is a distance ("SL@S 8+"), not a price
NOTE_WINDOW_MIN = 20        # the decision note is the first journal note this soon after it

_NUM = r"(\d+(?:\.\d+)?)"


def parse_plan(why: str, direction: str | None = None, when: str | None = None) -> dict:
    t = why or ""
    sl = re.search(r"\bSL(?:@S)?\s*[:=]?\s*[<>~]?\s*" + _NUM + r"(?![\d.]*\s*\+)", t, re.I)
    tp = re.search(r"\bTP\s*[:=]?\s*[<>~]?\s*" + _NUM + r"(?![\d.]*\s*\+)", t, re.I)
    b, s = re.search(r"\b(buy|long)\b", t, re.I), re.search(r"\b(sell|short)\b", t, re.I)
    side = ("buy" if b and (not s or b.start() < s.start()) else "sell") if (b or s) else None
    if side is None and direction and when in ("reject", "close", "hold", "touch"):
        up = direction == "above"
        side = ("sell" if up else "buy") if when == "reject" else ("buy" if up else "sell")
    two_step = bool(re.search(r"\bthen\s+(the\s+)?retest\b", t, re.I))
    return {"side": side, "sl": float(sl.group(1)) if sl else None, "tp": float(tp.group(1)) if tp else None,
            "two_step": two_step}


def _t(s) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(str(s)[:16], "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def passes(rows: list[dict], day: str, day_of, note_types=("note", "journal")) -> list[dict]:
    """The day's met alerts with no order on that symbol within ORDER_WINDOW_MIN, each with its decision note."""
    hits = [r for r in rows if r.get("type") == "level_alert" and day_of(r) == day]
    out = []
    for h in hits:
        t0 = _t(h.get("touched_utc") or h.get("utc"))
        if t0 is None:
            continue
        taken = any(r.get("type") == "order_placed" and r.get("symbol") == h.get("symbol")
                    and (tt := _t(r.get("utc") or r.get("logged_utc"))) and t0 <= tt <= t0 + dt.timedelta(minutes=ORDER_WINDOW_MIN)
                    for r in rows)
        if taken:
            continue
        note = next((r.get("text") for r in rows if r.get("type") in note_types
                     and (tt := _t(r.get("utc") or r.get("logged_utc"))) and t0 <= tt <= t0 + dt.timedelta(minutes=NOTE_WINDOW_MIN)), None)
        out.append({**h, "decision_note": note})
    return out


def grade(p: dict, after: list[dict]) -> dict:
    """Replay a pass on the M5 bars after the alert bar (rows like bars_view: utc, o, h, l, c), entry at the
    alert bar's close, the plan's own stop and target; a bar that reaches both counts as the stop (no peeking
    inside a bar)."""
    plan = parse_plan(p.get("why", ""), p.get("dir"), p.get("when"))
    if p.get("plan_sl") is not None and p.get("plan_tp") is not None:   # the alert's own mock levels (2026-10-18)
        plan = {**plan, "side": p.get("plan_side") or plan["side"], "sl": float(p["plan_sl"]),
                "tp": float(p["plan_tp"]), "two_step": False}
    entry = (p.get("bar") or {}).get("c")
    base = {"id": p.get("id"), "symbol": p.get("symbol"), "at": p.get("touched_utc"), "plan": plan, "entry": entry}
    if plan["two_step"]:
        return {**base, "result": "step 1 of a two-step plan (then retest): not an entry, not graded"}
    for k in ("sl", "tp"):
        if entry and plan[k] is not None and abs(plan[k] - entry) > PLAUSIBLE * abs(entry):
            plan[k] = None
    if entry is None or plan["side"] is None or plan["sl"] is None or plan["tp"] is None:
        missing = [k for k in ("side", "sl", "tp") if plan[k] is None] + (["entry"] if entry is None else [])
        return {**base, "result": f"not graded: the plan states no {', '.join(missing)} as a price"}
    buy = plan["side"] == "buy"
    risk, reward = (entry - plan["sl"], plan["tp"] - entry) if buy else (plan["sl"] - entry, entry - plan["tp"])
    if risk <= 0 or reward <= 0:
        return {**base, "result": f"not graded: stop {plan['sl']} / target {plan['tp']} are not on the right sides "
                                  f"of the entry {entry} for a {plan['side']}"}
    mfe = mae = 0.0
    t0 = str(p.get("touched_utc") or "")[:16]
    t_end = _t(t0) + dt.timedelta(hours=HORIZON_H) if _t(t0) else None
    after = [b for b in after if str(b.get("utc", ""))[:16] > t0 and (t_end is None or _t(b.get("utc")) <= t_end)]
    for b in after:
        fav, adv = ((b["h"] - entry), (entry - b["l"])) if buy else ((entry - b["l"]), (b["h"] - entry))
        mfe, mae = max(mfe, fav / risk), max(mae, adv / risk)
        if adv >= risk:
            return {**base, "rr": round(reward / risk, 2), "result": f"STOP first at {b['utc']} UTC: -1R "
                    f"(best on the way +{mfe:.2f}R)"}
        if fav >= reward:
            return {**base, "rr": round(reward / risk, 2), "result": f"TARGET first at {b['utc']} UTC: "
                    f"+{reward / risk:.2f}R (worst on the way -{mae:.2f}R)"}
    last = after[-1]["utc"] if after else "-"
    return {**base, "rr": round(reward / risk, 2), "result": f"neither by {last} UTC (best +{mfe:.2f}R, worst -{mae:.2f}R)"}


def text(graded: list[dict]) -> str:
    if not graded:
        return ""
    lines = [f"PASSES TODAY: {len(graded)} plan(s) whose alert was MET and that were NOT traded, each replayed by code "
             "on the bars that followed (entry at the alert bar's close, the plan's own stop and target; a bar "
             "reaching both counts as the stop):"]
    for g in graded:
        lines.append(f"- alert {g['id']} {g['symbol']} at {g['at']}: {g['result']}"
                     + (f" | plan R:R {g['rr']}" if g.get("rr") else "")
                     + f"\n    plan: {g.get('why', '')}\n    decision note: {g.get('decision_note') or '(none found)'}")
    return "\n".join(lines)


TASK = """GRADE EACH PASS above (the code result is fact; the judgement is yours). For each: was passing right? A
pass that avoided a stop is a rule working; a pass that missed a target is a rule -- or a trigger written looser
than you would act on -- costing money. Name what made you pass (a checklist line, an anti-pattern code, R:R, a
session, a margin like "pierce only 1 pt") and say whether it should STAY, move INTO the alert's own condition
(so the alert only fires when you would act), or GO. Then post ONE lesson per symbol that taught something
(board post --from review --topic lesson --symbol S --text "..."): the next trader at that level reads it."""


def graded(rows: list[dict], day: str, day_of, bars_after_for, note_types=("note", "journal")) -> list[dict]:
    out = []
    for p in passes(rows, day, day_of, note_types):
        g = grade(p, bars_after_for(p["symbol"]))
        out.append({**g, "why": p.get("why"), "decision_note": p.get("decision_note")})
    return out


def day_block(rows: list[dict], day: str, day_of, bars_after_for, note_types=("note", "journal")) -> tuple[int, str]:
    """(how many passes, the text block). bars_after_for(symbol) -> M5 rows (utc, o, h, l, c) up to now."""
    gs = graded(rows, day, day_of, bars_after_for, note_types)
    real = [g for g in gs if not g["result"].startswith("step 1")]
    return len(real), text(gs) + ("\n" + TASK if real else "")
