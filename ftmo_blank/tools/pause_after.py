"""PAUSE AFTER A TRAINED DAY (2026-10-18, operator: "let it run for a trained day, then pause and evaluate").

Watches the training queue and pauses the replay -- the dashboard's own markers, state/MANUAL_PAUSE + state/STOP
-- once the day review of DAY (and, if --with-calibration, a calibration review) has finished. STOP lets the call
in flight finish; the engine halts before the next one, and the resume timer leaves a MANUAL_PAUSE alone.
Safety stop: the replay's clock passing --latest. Run as a transient user service so it outlives usage-limit
pauses:

  systemd-run --user --unit ftmo-pause-after-day python3 tools/pause_after.py --day 2025-06-16 --with-calibration
  systemd-run --user --unit ftmo-pause-at-limit python3 tools/pause_after.py --at-usage-limit
--at-usage-limit (2026-10-18, operator: "leave it training until usage limits hit"): the first usage-limit pause the
replay takes on its own becomes a MANUAL pause, so it does NOT resume when the window resets; the dashboard's
Resume brings it back.
"""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]


def done(db: Path, needle: str, since: float) -> bool:
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT COUNT(*) FROM demands WHERE created > ? AND status = 'done' AND prompt LIKE ?",
                         (since, f"%{needle}%")).fetchone()[0] > 0
    finally:
        c.close()


def sim_now() -> str:
    try:
        with open(HOME / "state" / "ledger.jsonl", "rb") as f:
            f.seek(-4096, 2)
            last = f.read().decode(errors="ignore").strip().splitlines()[-1]
        return json.loads(last).get("utc", "")
    except (OSError, ValueError, IndexError):
        return ""


def pause(why: str) -> None:
    (HOME / "state" / "MANUAL_PAUSE").write_text(f"paused for the operator (tools/pause_after.py): {why}\n")
    (HOME / "state" / "STOP").write_text("paused for the operator\n")
    print(f"{time.strftime('%H:%M:%S')} paused: {why}", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day")
    ap.add_argument("--at-usage-limit", action="store_true")
    ap.add_argument("--with-calibration", action="store_true")
    ap.add_argument("--latest", default=None, help="safety: pause once the replay's clock passes this (YYYY-MM-DD)")
    ap.add_argument("--every", type=float, default=15.0)
    a = ap.parse_args(argv)
    since = time.time()
    db = HOME / "state" / "selfcall.db"
    if a.at_usage_limit:
        marker = HOME / "state" / "paused_until.json"
        print("watching for the first usage-limit pause", flush=True)
        while True:
            try:
                if marker.exists() and json.loads(marker.read_text()).get("paused_at", 0) >= since - 5:
                    pause("the usage limit was hit; held until the operator resumes it")
                    return 0
            except (OSError, ValueError):
                pass
            time.sleep(a.every)
    if not a.day:
        ap.error("--day is required unless --at-usage-limit")
    print(f"watching for the day review of {a.day}" + (" and a calibration review" if a.with_calibration else ""),
          flush=True)
    while True:
        day_ok = done(db, f"DAY CLOSED {a.day}", since)
        cal_ok = (not a.with_calibration) or done(db, "CALIBRATION REVIEW", since)
        if day_ok and cal_ok:
            pause(f"day review of {a.day}" + (" and the calibration review" if a.with_calibration else "") + " done")
            return 0
        if a.latest and sim_now()[:10] > a.latest:
            pause(f"safety: the replay passed {a.latest} ({sim_now()})")
            return 0
        time.sleep(a.every)


if __name__ == "__main__":
    sys.exit(main())
