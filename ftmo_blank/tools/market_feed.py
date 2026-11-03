"""MARKET FEED (distributor): one snapshot fetched once per tick, published for everyone to read.

See manual/LIVE_TRADING_IDEA.md section 2. In TRAINING, driver.py already hands trade's own
prompt a fresh Paper().snapshot() every turn -- this does not change that, it publishes the SAME
snapshot alongside it, so any kind (or a future one) can read current market state with one cheap
read instead of re-deriving it. In LIVE (2026-10-11), tools/live_loop.py publishes the real
account snapshot (tools/live_broker.py snapshot) once per tick here; called with no snapshot in
hand it reads one from the broker itself, and if the broker cannot be reached it records that
error plainly rather than fabricating a snapshot or leaving the last one looking fresh.

  market_feed.py distribute                 (driver.py calls this once per turn) publish the current snapshot
  market_feed.py read                        the last published snapshot, with its own age in seconds
"""
import json
import sys
import time
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "tools"))
FEED_PATH = HOME / "state" / "market_feed.json"


def distribute(mode: str = "TRAINING", home: Path = HOME, snapshot: dict | None = None) -> dict:
    """snapshot, if given (driver.py already has one every turn -- constructing a second Paper()
    here just to get the same numbers again would mean reloading every symbol's feed from disk
    a second time, pointlessly), is published as-is. Only the CLI / a caller with no snapshot in
    hand yet builds its own Paper() to get one."""
    now = time.time()
    if mode == "TRAINING":
        if snapshot is None:
            import paper as P
            snapshot = P.Paper(home=home).snapshot()
        rec = {"mode": mode, "published_utc": snapshot.get("utc"), "published_at": now, "snapshot": snapshot}
    elif mode == "LIVE":
        err = None
        if snapshot is None:
            try:
                import live_broker as LB
                snapshot = LB.snapshot()
            except Exception as exc:
                err = f"live broker snapshot failed: {exc}"
        rec = {"mode": mode, "published_utc": (snapshot or {}).get("utc"), "published_at": now,
               "snapshot": snapshot}
        if err:
            rec["error"] = err
    else:
        rec = {"mode": mode, "published_utc": None, "published_at": now, "snapshot": None,
               "error": f"nothing to publish in mode {mode}"}
    path = Path(home) / "state" / "market_feed.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rec))
    return rec


def read(home: Path = HOME) -> dict:
    path = Path(home) / "state" / "market_feed.json"
    if not path.exists():
        return {"error": "nothing published yet"}
    rec = json.loads(path.read_text())
    rec["age_s"] = round(time.time() - rec.get("published_at", 0), 1)
    return rec


def main(argv=None, home=HOME) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "distribute":
        mode = argv[1] if len(argv) > 1 else "TRAINING"
        print(json.dumps(distribute(mode, home), indent=1, default=str))
    elif argv and argv[0] == "read":
        print(json.dumps(read(home), indent=1, default=str))
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
