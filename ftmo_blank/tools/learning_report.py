"""LEARNING REPORT (2026-10-18, operator: "evaluate how it is LEARNING, not so much the result -- plus trade
QUANTITY: how many opportunities it takes, and whether that number shifts BECAUSE of learned patterns").

Reads the training desk's own records for a stretch of simulated days and prints, per stretch and per day:
  flow      movers flagged, scouts, decision turns, alerts set / met / taken / passed, orders, closed trades
  process   verdicts (VERDICT lines), reason codes, how often the VERDICT format is used, planned R:R at entry
  graded    what the code-graded rejections and passes say (tools/opportunities.py rows)
  learning  lessons, calibration lessons, papers, dreams notes, evolves -- and whether decisions CITE the lessons
            and calibration (card ids L.., P:.., C:..)
  cost      calls and $ by kind (by real time window, --since / --until epoch seconds)

  python3 tools/learning_report.py --from 2025-05-27 --to 2025-06-12 [--vs-from 2025-06-13 --vs-to 2025-06-17]
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import sys
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(HOME / "tools"), str(HOME / "agent_code"), str(HOME)]

VERDICT_RE = re.compile(r"VERDICT\s+(\S+)\s+(NOW|ALERT|WATCH|DISMISS|PASS|SKIP)\b([^\n|]*)", re.I)
CITE_RE = re.compile(r"\b(L\d{1,5}|LL\d{1,5}|P:[a-z0-9_\-]+|C:[A-Za-z0-9.]+)\b")


def _day(utc: str) -> str:
    import kstore as K
    return K.rec_day({"utc": utc}) or str(utc)[:10]


def stretch(frm: str, to: str, home=HOME) -> dict:
    import kstore as K
    import board as TB
    import opportunities as OPP
    led = [r for r in K.visible_ledger(home) if frm <= (K.rec_day(r) or "") <= to]
    posts = [p for p in TB.read(n=0, home=home) if frm <= _day(p.get("utc", "")) <= to]
    opp = [r for r in OPP.load("training", home) if frm <= r.get("day", "") <= to]
    days = sorted({K.rec_day(r) for r in led if r.get("type") == "day_closed"} | {r.get("day") for r in opp})
    trading_days = [d for d in days if d and __import__("datetime").date.fromisoformat(d).weekday() < 5]
    by = collections.Counter(r.get("type") for r in led)
    movers = sum(len(r.get("candidates") or []) for r in led if r.get("type") == "setup_candidate")
    orders = [r for r in led if r.get("type") == "order_placed"]
    closed = [r for r in led if r.get("type") == "trade_closed"]
    rr = []
    for o in orders:
        e = o.get("ref_price") or o.get("est_fill") or o.get("entry")
        if e and o.get("sl") and o.get("tp"):
            risk, rew = abs(e - o["sl"]), abs(o["tp"] - e)
            if risk > 0:
                rr.append(rew / risk)
    verdicts = collections.Counter()
    codes = collections.Counter()
    scout_posts = [p for p in posts if p.get("worker") == "scout"]
    with_line = 0
    for p in posts:
        found = VERDICT_RE.findall(p.get("text", ""))
        if found and p.get("worker") == "scout":
            with_line += 1
        for _, w, rest in found:
            verdicts[w.upper()] += 1
            codes.update(re.findall(r"\b([SA][1-5])\b", rest))
    lessons = [p for p in posts if "lesson" in str(p.get("topic", "")).lower()]
    calib_lessons = [p for p in lessons if "calibrat" in p.get("text", "").lower() or "C:" in p.get("text", "")]
    cites = collections.Counter()
    decisions = [p for p in posts if p.get("worker") in ("trade",) or p.get("topic") in ("decision", "pre_trade")]
    cited = 0
    for p in decisions:
        ids = CITE_RE.findall(p.get("text", ""))
        if ids:
            cited += 1
            cites.update(i.split(":")[0] if ":" in i else re.sub(r"\d+", "", i) for i in ids)
    # did the woken trader set ITS OWN levels, or copy the alert's mock ones? (2026-10-18)
    import datetime as _dt
    anchored, woken = 0, 0
    hits = [r for r in led if r.get("type") == "level_alert" and r.get("plan_sl") is not None]
    for o in orders:
        t_o = K._utc(o.get("utc"))
        h = next((x for x in reversed(hits) if x.get("symbol") == o.get("symbol") and t_o and K._utc(x.get("utc"))
                  and _dt.timedelta(0) <= t_o - K._utc(x.get("utc")) <= _dt.timedelta(minutes=30)), None)
        if h:
            woken += 1
            same = lambda a, b: a is not None and b is not None and abs(float(a) - float(b)) <= 1e-4 * abs(float(b))
            anchored += same(o.get("sl"), h["plan_sl"]) and same(o.get("tp"), h["plan_tp"])
    movers_g = [r for r in opp if r.get("kind") == "mover"]
    passes_g = [r for r in opp if r.get("kind") == "pass"]
    return {"from": frm, "to": to, "trading_days": len(trading_days), "days": trading_days,
            "movers_flagged": movers, "scans": by.get("setup_candidate", 0),
            "alerts_set": by.get("alert_set", 0), "alerts_met": by.get("level_alert", 0),
            "alerts_expired": by.get("alert_expired", 0), "alerts_cancelled": by.get("alert_cancelled", 0),
            "orders": len(orders), "closed": len(closed),
            "closed_R": [round(r.get("r_multiple") or 0, 2) for r in closed],
            "planned_rr": round(sum(rr) / len(rr), 2) if rr else None,
            "notes": by.get("note", 0), "verdicts": dict(verdicts), "codes": dict(codes.most_common(8)),
            "scout_posts": len(scout_posts), "scout_posts_with_verdict_line": with_line,
            "lessons": len(lessons), "calibration_lessons": len(calib_lessons),
            "decision_posts": len(decisions), "decision_posts_citing_cards": cited, "cited_kinds": dict(cites),
            "alert_orders": woken, "alert_orders_copying_mock_levels": anchored,
            "graded_movers": len(movers_g), "graded_passes": len(passes_g),
            "graded_summary": OPP.summarize(opp, "graded in this stretch") if opp else "(none graded)"}


def per_day(frm: str, to: str, home=HOME) -> list[dict]:
    import kstore as K
    days = sorted({K.rec_day(r) for r in K.visible_ledger(home)
                   if frm <= (K.rec_day(r) or "") <= to and r.get("type") in ("setup_candidate", "order_placed")})
    out = []
    for d in days:
        s = stretch(d, d, home)
        out.append({k: s[k] for k in ("from", "movers_flagged", "alerts_set", "alerts_met", "orders", "closed_R",
                                      "verdicts", "lessons", "decision_posts_citing_cards")})
    return out


def costs(since: float, until: float | None = None, home=HOME) -> dict:
    c = sqlite3.connect(Path(home) / "state" / "selfcall.db")
    q = "SELECT kind, COUNT(*), COALESCE(SUM(cost_usd),0) FROM demands WHERE started >= ? AND status='done'"
    args = [since]
    if until:
        q += " AND started < ?"
        args.append(until)
    rows = c.execute(q + " GROUP BY kind", args).fetchall()
    return {"calls": sum(r[1] for r in rows), "usd": round(sum(r[2] for r in rows), 2),
            "by_kind": {r[0]: [r[1], round(r[2], 2)] for r in rows}}


def text(s: dict) -> str:
    td = max(1, s["trading_days"])
    vl = f"{s['scout_posts_with_verdict_line']}/{s['scout_posts']}" if s["scout_posts"] else "-"
    return "\n".join([
        f"## {s['from']} .. {s['to']} ({s['trading_days']} trading days)",
        f"- flow: {s['movers_flagged']} movers flagged ({s['movers_flagged'] / td:.1f}/day), alerts set {s['alerts_set']}, "
        f"met {s['alerts_met']}, expired {s['alerts_expired']}, cancelled {s['alerts_cancelled']}; orders {s['orders']} "
        f"({s['orders'] / td:.2f}/day), closed {s['closed']} R {s['closed_R']}; planned R:R at entry {s['planned_rr']}",
        f"- process: verdicts {s['verdicts']}; reason codes {s['codes']}; scout posts with a VERDICT line {vl}; "
        f"journal notes {s['notes']}",
        f"- mock levels: {s['alert_orders']} order(s) after an alert with mock levels, "
        f"{s['alert_orders_copying_mock_levels']} copied both mock levels exactly",
        f"- learning: lessons {s['lessons']} (calibration lessons {s['calibration_lessons']}); decision posts citing "
        f"cards {s['decision_posts_citing_cards']}/{s['decision_posts']} {s['cited_kinds']}",
        f"- graded: {s['graded_movers']} rejected movers, {s['graded_passes']} passes\n" +
        "\n".join("    " + l for l in s["graded_summary"].splitlines())])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", required=True)
    ap.add_argument("--to", required=True)
    ap.add_argument("--vs-from")
    ap.add_argument("--vs-to")
    ap.add_argument("--days", action="store_true", help="also one line per day")
    a = ap.parse_args(argv)
    print(text(stretch(a.frm, a.to)))
    if a.days:
        for d in per_day(a.frm, a.to):
            print(" ", json.dumps(d))
    if a.vs_from and a.vs_to:
        print(text(stretch(a.vs_from, a.vs_to)))
        if a.days:
            for d in per_day(a.vs_from, a.vs_to):
                print(" ", json.dumps(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
