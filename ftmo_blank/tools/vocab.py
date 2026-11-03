"""THE DESK VOCABULARY: shared codes for setups, anti-patterns, structure, levels, triggers and verdicts
(knowledge/vocab.json). Every worker, training and live, writes posts, alert plans and journal notes in
these codes and looks one up here instead of re-explaining it in prose. Read-only.

  vocab.py index                 every code with its short name, one line per category
  vocab.py lookup CODE [CODE..]  full definitions (case-insensitive), e.g. lookup S1 A2 T-HOLD
  vocab.py find WORDS            search names and definitions
  vocab.py grammar               how a post, a dismissal and an alert plan are written
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
VOCAB = HOME / "knowledge" / "vocab.json"
CATS = ("setup", "anti", "structure", "state", "level", "trigger", "plan", "verdict", "risk")


def load(path: Path = VOCAB) -> dict:
    return json.loads(path.read_text())


def index(v: dict) -> str:
    """The compact list the prompts carry: `setup: S1 ACCEPT-CONT, S2 FAILED-CTR, ...`."""
    lines = []
    for cat in CATS:
        items = [f"{k} {t['name']}" if t["name"].upper() != k else k for k, t in v["terms"].items() if t["cat"] == cat]
        if items:
            lines.append(f"{cat}: " + ", ".join(items))
    return "\n".join(lines)


def lookup(v: dict, codes: list[str]) -> str:
    terms = {k.upper(): (k, t) for k, t in v["terms"].items()}
    out = []
    for c in codes:
        hit = terms.get(c.upper())
        if hit is None:
            out.append(f"{c}: not a desk code (vocab.py find WORDS searches)")
            continue
        k, t = hit
        extra = " | ".join(f"{f}: {t[f]}" for f in ("entry", "stop", "target", "instead", "record") if t.get(f))
        out.append(f"{k} [{t['cat']}] {t['name']}: {t['def']}" + (f" | {extra}" if extra else ""))
    return "\n".join(out)


def find(v: dict, words: list[str]) -> str:
    ws = [w.lower() for w in words]
    hits = [k for k, t in v["terms"].items()
            if all(w in (k + " " + t["name"] + " " + t["def"]).lower() for w in ws)]
    return lookup(v, hits) if hits else "no code matches; describe it in a few words in the post instead"


def prompt_block(v: dict | None = None) -> str:
    """The one vocabulary paragraph BOTH routers put in every prompt (agent_code/routing.py for training,
    tools/live_routing.py for live) -- one source, so the two desks cannot drift apart on what a code means."""
    v = v or load()
    return (f"- DESK VOCABULARY (shared by every worker, training and live): write board posts, alert plans and journal\n"
            f"  notes in these codes and this grammar; never re-explain a code -- look one up with python3 {Path(__file__).resolve()}\n"
            f"  lookup CODE (find WORDS searches; grammar shows every format). Post: {v['grammar']['post'].split('   ')[0]}\n  "
            + index(v).replace("\n", "\n  "))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="vocab.py", description="the desk vocabulary (read-only)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("index")
    lk = sub.add_parser("lookup")
    lk.add_argument("codes", nargs="+")
    fd = sub.add_parser("find")
    fd.add_argument("words", nargs="+")
    sub.add_parser("grammar")
    a = ap.parse_args(argv)
    v = load()
    if a.cmd == "lookup":
        print(lookup(v, a.codes))
    elif a.cmd == "find":
        print(find(v, a.words))
    elif a.cmd == "grammar":
        print("\n".join(f"{k}: {g}" for k, g in v["grammar"].items()))
    else:
        print(index(v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
