"""RESUME CHECK: brings the replay service back after a usage-limit pause, at the right time.

Run every few minutes by the ftmo-field-resume systemd timer (not by hand, though it is safe to).
Does nothing unless state/paused_until.json says the wait is over, and never overrides a pause the
operator set on purpose (state/MANUAL_PAUSE) or a kill (state/KILLED).
"""
import json
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
STATE = HOME / "state"


def main() -> int:
    if (STATE / "KILLED").exists():
        print("killed: not auto-resuming (ask the operator)")
        return 0
    if (STATE / "MANUAL_PAUSE").exists():
        print("manually paused: not auto-resuming (use the dashboard Resume button)")
        return 0
    marker = STATE / "paused_until.json"
    if not marker.exists():
        return 0
    mode = (STATE / "MODE").read_text().strip() if (STATE / "MODE").exists() else "TRAINING"
    if mode in ("OFF", "LIVE"):
        # the replay's limit pause is moot once the master switch left TRAINING; switching back to
        # TRAINING starts it from the dashboard. Never start the replay next to the live loop.
        print(f"mode is {mode}: not resuming the replay")
        return 0
    data = json.loads(marker.read_text())
    resume_at = data.get("resume_at", 0)
    if time.time() < resume_at:
        left = int(resume_at - time.time())
        print(f"still waiting: {left}s left ({data.get('reason', '')})")
        return 0
    marker.unlink()
    stop = STATE / "STOP"
    if stop.exists():
        stop.unlink()
    subprocess.run(["systemctl", "--user", "start", "ftmo-field.service"])
    print(f"resumed after a {data.get('reason', 'limit')} pause")
    return 0


if __name__ == "__main__":
    sys.exit(main())
