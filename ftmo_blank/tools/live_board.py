"""LIVE BOARD: the live assembly's own append-only meeting log (state/live_board.jsonl).

Why not board.py: board.py stamps a post with the SIMULATED time and hides every post dated
after it (so an epoch wrap cannot leak the future). A live post carries the real wall-clock
time, which is always after the replay's clock -- through board.py every live post would be
stamped with a 2025 replay date and every real-time one would read as invisible. Live gets its
own file, stamped with true UTC, and the training board stays exactly as it was.

What carries over from training is what training CONCLUDED: `lessons` walks the training board's review
lessons, the papers and the live board's own lessons by situation (tools/pathways.py, the same walk as
board.py lessons on the training desk) and prints the ones on the path in full. They are about process,
not about today's prices.

  live_board.py post --from WORKER --topic T --text "..." [--symbol S]
  live_board.py read [--worker W] [--topic T] [--symbol S] [-n 20]     one line per post (board.read_text)
  live_board.py lessons [--symbol S] [--codes S1,A1] [--phase P] [--session S] [-n N] | --show ID | --map

Same guarantee as board.py: no edit or delete verb exists, for anyone.
"""
import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import board as TRAINING_BOARD  # noqa: E402

WORKERS = TRAINING_BOARD.WORKERS
DAY_TZ = "Europe/Prague"


def _path(home) -> Path:
    return Path(home) / "state" / "live_board.jsonl"


def _now() -> dt.datetime:
    try:  # the same clock live_broker.py reads, so a consulted_analysis check agrees with the order's day
        import live_broker
        return live_broker._now_utc()
    except Exception:
        return dt.datetime.now(dt.timezone.utc)


def post(worker: str, topic: str, text: str, symbol: str | None = None, home=HOME) -> dict:
    if worker not in WORKERS:
        raise SystemExit(f"unknown worker {worker!r}; one of {WORKERS}")
    topic, text = topic.strip(), text.strip()
    if not topic or not text:
        raise SystemExit("--topic and --text are both required and may not be blank")
    now = _now()
    rec = {"utc": now.strftime("%Y-%m-%d %H:%M UTC"), "day": now.astimezone(ZoneInfo(DAY_TZ)).date().isoformat(),
           "worker": worker, "topic": topic, "text": text}
    if symbol:
        rec["symbol"] = symbol.upper()
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def read(worker: str | None = None, topic: str | None = None, symbol: str | None = None,
         n: int = 20, home=HOME) -> list[dict]:
    path = _path(home)
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if worker and rec.get("worker") != worker:
            continue
        if topic and topic.lower() not in str(rec.get("topic", "")).lower():
            continue
        if symbol and rec.get("symbol") != symbol.upper():
            continue
        out.append(rec)
    return out[-n:] if n > 0 else out


def read_text(posts: list[dict]) -> str:
    """The training board's one-line-per-post form (2026-10-18), so both desks read posts the same way."""
    return TRAINING_BOARD.read_text(posts)


def consulted(symbol: str, home=HOME) -> bool:
    """A pre_trade post -- or a specialist's consult answer (tools/consult.py, 2026-10-19) -- for this symbol on the
    live board, dated today (Prague day)."""
    today = _now().astimezone(ZoneInfo(DAY_TZ)).date().isoformat()
    return any(p.get("day") == today for t in ("pre_trade", "consult")
               for p in read(topic=t, symbol=symbol, n=50, home=home))


def main(argv=None, home=HOME) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["lessons"]:   # 2026-10-18: lessons by situation (was: the last N by date)
        import pathways as PW
        print(PW.cli(argv[1:], "live", home, f"python3 {HOME}/tools/live_board.py lessons"))
        return 0
    p = argparse.ArgumentParser(prog="live_board.py", description="the live assembly's append-only meeting log")
    sub = p.add_subparsers(dest="cmd")
    po = sub.add_parser("post")
    po.add_argument("--from", dest="worker", required=True, choices=WORKERS)
    po.add_argument("--topic", required=True)
    po.add_argument("--text", required=True)
    po.add_argument("--symbol")
    r = sub.add_parser("read")
    r.add_argument("--worker", choices=WORKERS)
    r.add_argument("--topic")
    r.add_argument("--symbol")
    r.add_argument("-n", type=int, default=20)
    a = p.parse_args(argv)
    if a.cmd == "post":
        rec = post(a.worker, a.topic, a.text, a.symbol, home)
        # a short receipt, as board.py: the poster already has its text
        print(json.dumps({"ok": True, "utc": rec["utc"], "worker": rec["worker"], "topic": rec["topic"],
                          "symbol": rec.get("symbol"), "chars": len(rec["text"])}, separators=(",", ":")))
    elif a.cmd == "read":
        print(read_text(read(a.worker, a.topic, a.symbol, a.n, home)))
    else:
        print(__doc__)
        print(json.dumps({"live_posts_so_far": len(read(n=0, home=home))}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
