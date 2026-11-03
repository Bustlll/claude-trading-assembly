"""BOARD: the shared "meeting" log the specialist workers post to and read from.

Genuinely append-only: the only write verb is `post`, which adds one line. There is no
edit or delete command here, and the guard gives no kind raw Write/Edit on state/board.jsonl
(only this tool's post/read) -- so nothing, not even the worker that posted it, can change
or erase a post. That is the whole enforcement: no verb exists to do it, for anyone.

  board.py post --from WORKER --topic TOPIC --text "..." [--symbol SYM]
  board.py read [--worker W] [--topic T] [--symbol S] [-n 20]
  board.py lessons [--symbol S] [--codes S1,A1] [--phase P] [--session S] [-n N] | --show ID | --map
                                             the lessons on a situation's path (tools/pathways.py), in full
  board.py                                   this doc, plus how many posts exist

WORKERS, each a specialist with its own kind in guard/policy.py:
  scout   screens symbols for a candidate worth a closer look (breadth, not depth)
  chart   a deep multi-timeframe read of the symbols scout (or trade) flagged
  timing  session, day-of-week, pacing within the window, and the FTMO clock
  review  what a closed trade or day taught, after the fact
  trade   the only one that reads positions and places orders; reads the board before deciding

Posts are filtered on read the same way the ledger is (see agent_code/kstore.py now_utc): a
post dated after the current simulated time (possible only right after an epoch wraps back to
an earlier window) is invisible, so an epoch wrap can never leak a specialist's own future.
"""
import argparse
import json
import sys
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "agent_code"))
import kstore as K  # noqa: E402  (reuses now_utc/_utc so the blindness rule is exactly the ledger's)
sys.path.insert(0, str(HOME / "tools"))

WORKERS = ("scout", "chart", "timing", "review", "trade", "manage", "vault")


def _board_path(home: Path) -> Path:
    return Path(home) / "state" / "board.jsonl"


def _worker(name: str) -> str:
    """A symbol trader posts as itself (trade_us100_cash): it is a trade worker (2026-11-01: those posts were refused)."""
    return "trade" if str(name).startswith("trade_") else name


def post(worker: str, topic: str, text: str, symbol: str | None = None, home=HOME) -> dict:
    worker = _worker(worker)
    if worker not in WORKERS:
        raise SystemExit(f"unknown worker {worker!r}; one of {WORKERS}")
    topic, text = topic.strip(), text.strip()
    if not topic or not text:
        raise SystemExit("--topic and --text are both required and may not be blank")
    now = K.now_utc(home)
    rec = {"utc": now.strftime("%Y-%m-%d %H:%M UTC") if now else "", "worker": worker, "topic": topic, "text": text}
    if symbol:
        rec["symbol"] = symbol.upper()
    path = _board_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def read(worker: str | None = None, topic: str | None = None, symbol: str | None = None,
         n: int = 20, home=HOME) -> list[dict]:
    path = _board_path(home)
    if not path.exists():
        return []
    now = K.now_utc(home)
    out = []
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = K._utc(rec.get("utc"))
        if now is not None and t is not None and t > now:
            continue  # posted in a window of a later epoch than the one being replayed now
        if worker and rec.get("worker") != worker:
            continue
        if topic and topic.lower() not in str(rec.get("topic", "")).lower():
            continue
        if symbol and rec.get("symbol") != symbol.upper():
            continue
        out.append(rec)
    return out[-n:] if n > 0 else out


def read_text(posts: list[dict]) -> str:
    """Posts as one line each (2026-10-18): `[utc] worker/topic SYMBOL: text`. The indent=1 JSON list
    repeated four key names and quoting per post; board reads are a third of what the workers' tool
    results cost. Same fields, nothing dropped."""
    if not posts:
        return "(no posts match)"
    return "\n".join(f"[{p.get('utc', '')}] {p.get('worker', '?')}/{p.get('topic', '?')}"
                     f"{' ' + p['symbol'] if p.get('symbol') else ''}: {p.get('text', '')}" for p in posts)


def main(argv=None, home=HOME) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["lessons"]:   # 2026-10-18: lessons by situation, same walk as live_board.py lessons
        import pathways as PW
        print(PW.cli(argv[1:], "training", home, f"python3 {HOME}/tools/board.py lessons"))
        return 0
    p = argparse.ArgumentParser(prog="board.py", description="the shared, append-only meeting log")
    sub = p.add_subparsers(dest="cmd")
    po = sub.add_parser("post", help="append one finding (no edit, no delete -- ever)")
    po.add_argument("--from", dest="worker", required=True, type=_worker, choices=WORKERS)
    po.add_argument("--topic", required=True)
    po.add_argument("--text", required=True)
    po.add_argument("--symbol")
    r = sub.add_parser("read", help="recent posts, optionally filtered")
    r.add_argument("--worker", choices=WORKERS)
    r.add_argument("--topic")
    r.add_argument("--symbol")
    r.add_argument("-n", type=int, default=20)
    a = p.parse_args(argv)

    if a.cmd == "post":
        rec = post(a.worker, a.topic, a.text, a.symbol, home)
        # 2026-10-18: a short receipt, not the whole post echoed back -- the poster already has its text
        print(json.dumps({"ok": True, "utc": rec["utc"], "worker": rec["worker"], "topic": rec["topic"],
                          "symbol": rec.get("symbol"), "chars": len(rec["text"])}, separators=(",", ":")))
    elif a.cmd == "read":
        print(read_text(read(a.worker, a.topic, a.symbol, a.n, home)))
    else:
        print(__doc__)
        print(json.dumps({"posts_so_far": len(read(n=0, home=home))}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
