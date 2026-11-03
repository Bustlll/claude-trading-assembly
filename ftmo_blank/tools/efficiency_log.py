"""EFFICIENCY LOG: how much replay one usage window buys, measured -- not estimated.

Every run appends one row to evals/efficiency/log.jsonl: wall time, the training login's 5h and weekly
meters (castl usage), training calls and API-equivalent cost since the start mark, and how far the replay
has moved in simulated business days. `summary` turns it into sim days per 1% of the 5h window, calls and
$ per sim day, next to the baseline of the old design (2026-10-17: 131 calls, $10.87, ~9 sim days for 100%
of one account_1 5h window). Read-only on everything but its own files; a systemd timer runs `record`.

  python3 tools/efficiency_log.py record [--account account_1]
  python3 tools/efficiency_log.py summary
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
OUT = HOME / "evals" / "efficiency"
BASELINE = {"calls": 131, "cost": 10.87, "sim_days": 9, "window_pct": 100}


def meters(account: str) -> tuple[float | None, float | None]:
    try:
        out = subprocess.run(["castl", "usage"], capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return None, None
    for line in out.splitlines():
        if line.startswith(account) and "5h" in line:
            a, b = re.search(r"5h\s+([\d.]+)%", line), re.search(r"week\s+([\d.]+)%", line)
            if a and b:
                return float(a.group(1)), float(b.group(1))
    return None, None


def sim_now() -> str | None:
    try:
        lines = (HOME / "state" / "turns.jsonl").read_text().splitlines()
        return json.loads(lines[-1]).get("at")
    except Exception:
        return None


def business_days(a: str, b: str) -> float:
    """Simulated business days between two 'YYYY-MM-DD HH:MM UTC' stamps (fractional, weekends skipped)."""
    da, db = (datetime.strptime(x[:16], "%Y-%m-%d %H:%M") for x in (a, b))
    if db <= da:
        return 0.0
    days, t = 0.0, da
    while t < db:
        nxt = min(db, datetime(t.year, t.month, t.day) + timedelta(days=1))
        if t.weekday() < 5:
            days += (nxt - t).total_seconds() / 86400
        t = nxt
    return round(days, 2)


def calls_since(t0: float) -> tuple[int, float, dict]:
    c = sqlite3.connect(HOME / "state" / "selfcall.db")
    rows = c.execute("SELECT kind, COUNT(*), COALESCE(SUM(cost_usd),0) FROM demands WHERE started>=? AND status='done' "
                     "GROUP BY kind", (t0,)).fetchall()
    return sum(r[1] for r in rows), round(sum(r[2] for r in rows), 3), {r[0]: r[1] for r in rows}


def record(account: str) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    start_path = OUT / "start.json"
    if not start_path.exists():
        h5, wk = meters(account)
        start_path.write_text(json.dumps({"t": time.time(), "sim": sim_now(), "h5": h5, "week": wk,
                                          "account": account}))
    st = json.loads(start_path.read_text())
    h5, wk = meters(account)
    n, cost, kinds = calls_since(st["t"])
    sim = sim_now()
    row = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "h5": h5, "week": wk, "calls": n, "cost": cost,
           "sim": sim, "sim_days": business_days(st["sim"], sim) if st.get("sim") and sim else None, "kinds": kinds}
    with open(OUT / "log.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")
    return row


def summary() -> str:
    rows = [json.loads(x) for x in (OUT / "log.jsonl").read_text().splitlines()]
    st = json.loads((OUT / "start.json").read_text())
    last = rows[-1]
    # the 5h meter resets: add up the rises, window by window
    used, prev = 0.0, st.get("h5") or 0.0
    for r in rows:
        if r["h5"] is None:
            continue
        used += r["h5"] - prev if r["h5"] >= prev else r["h5"]
        prev = r["h5"]
    sd = last.get("sim_days") or 0
    lines = [f"since {datetime.fromtimestamp(st['t']):%Y-%m-%d %H:%M} on {st['account']}: {last['calls']} calls, "
             f"${last['cost']:.2f} API-equivalent, {sd} sim business days ({st['sim']} -> {last['sim']}), "
             f"{used:.0f}% of a 5h window used, week now {last['week']}%"]
    if sd:
        lines.append(f"  new:      {last['calls'] / sd:.1f} calls/sim day, ${last['cost'] / sd:.2f}/sim day, "
                     + (f"{sd / used * 100:.1f} sim days per full 5h window" if used else "window use not measurable yet"))
    b = BASELINE
    lines.append(f"  baseline: {b['calls'] / b['sim_days']:.1f} calls/sim day, ${b['cost'] / b['sim_days']:.2f}/sim day, "
                 f"{b['sim_days'] / b['window_pct'] * 100:.1f} sim days per full 5h window (2026-10-17, old design)")
    lines.append(f"  calls by kind: {last['kinds']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="efficiency_log.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--account", default="account_1")
    sub.add_parser("summary")
    a = ap.parse_args(argv)
    print(json.dumps(record(a.account)) if a.cmd == "record" else summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
