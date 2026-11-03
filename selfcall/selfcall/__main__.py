"""Command line for the self-calling program.

  python -m selfcall enqueue research "question"   queue a demand
  python -m selfcall emit trade_closed '{"id": 7}'  turn an event into a demand
  python -m selfcall drain                          run until the queue is empty or halted
  python -m selfcall serve --poll 5                 run forever (the long-lived worker)
  python -m selfcall status | show ID               inspect the ledger
  python -m selfcall stop | resume                  halt / release the engine
"""
import argparse
import json
import sys

from .engine import KINDS, LIMITS, Engine, single_worker


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="selfcall")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enqueue", help="queue a root demand")
    e.add_argument("kind", choices=sorted(KINDS))
    e.add_argument("prompt")

    m = sub.add_parser("emit", help="emit an event as a demand")
    m.add_argument("event")
    m.add_argument("payload", nargs="?", default="{}")

    sub.add_parser("drain", help="run until idle or halted")
    s = sub.add_parser("serve", help="run forever")
    s.add_argument("--poll", type=float, default=5.0)

    sub.add_parser("status", help="counts, halt state, recent demands")
    sh = sub.add_parser("show", help="print one run record")
    sh.add_argument("id")
    sub.add_parser("stop", help="create the STOP file")
    sub.add_parser("resume", help="remove the STOP file")

    a = p.parse_args(argv)
    eng = Engine()

    if a.cmd == "enqueue":
        print(eng.enqueue(a.kind, a.prompt, source="cli"))
    elif a.cmd == "emit":
        print(eng.emit(a.event, json.loads(a.payload)))
    elif a.cmd in ("drain", "serve"):
        lock = single_worker(eng.home)  # keep the handle alive for the whole run
        if a.cmd == "drain":
            what, detail = eng.drain()
            print(f"{what}: {detail if detail is not None else ''}")
        else:
            eng.serve(a.poll)
        del lock
    elif a.cmd == "status":
        print("halt:", eng.halt_reason() or "none")
        n, cost = eng.store.totals()
        print(f"invocations {n}/{LIMITS['max_runs']}  cost ${cost:.4f}/${LIMITS['max_total_cost_usd']}")
        print("counts:", eng.store.counts())
        for r in eng.store.recent(10):
            print(f"  {r['id']} {r['kind']:8} {r['status']:8} d{r['depth']} {r['source']:<18} "
                  f"{r['prompt'][:70]!r}")
    elif a.cmd == "show":
        path = eng.runs / f"{a.id}.md"
        print(path.read_text() if path.exists() else f"no record for {a.id}")
    elif a.cmd == "stop":
        eng.stop_file.write_text("stopped by operator\n")
        print("STOP file written; the engine halts before its next invocation")
    elif a.cmd == "resume":
        if eng.stop_file.exists():
            eng.stop_file.unlink()
        print("STOP file removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
