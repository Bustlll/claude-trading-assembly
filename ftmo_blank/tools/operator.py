"""OPERATOR TOOL: human-only account control. Not in any agent permission rule, on purpose.

  python3 tools/operator.py status        attempt, epoch, window, pass rate
  python3 tools/operator.py skip-window    force the current attempt to end now and move to the next window

In backtest mode (now) attempts end and move on by themselves: this data is historical, so there is
nothing to "refresh". skip-window is a manual override for when the operator wants to abort a window
early (for example, after a code change). A later LIVE mode would need a real manual restart between
sessions; this tool is where that would go, but it is not wired to anything live yet.
"""
import json
import sys
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "tools"))
sys.path.insert(0, str(HOME))
import paper as P  # noqa: E402


def status(paper: P.Paper) -> dict:
    snap = paper.snapshot()
    return {k: snap[k] for k in ("attempt", "epoch", "window_index", "windows_per_epoch", "window_day",
                                 "attempts_total", "attempts_passed", "pass_rate", "equity", "breached")}


def skip_window(paper: P.Paper) -> dict:
    before = paper.s["attempt"]
    paper._end_window("operator_skip")
    paper.save()
    return {"ended_attempt": before, "now_attempt": paper.s["attempt"], "now_window": paper.s["window_index"]}


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    paper = P.Paper()
    if argv == ["status"]:
        print(json.dumps(status(paper), indent=1))
    elif argv == ["skip-window"]:
        print(json.dumps(skip_window(paper), indent=1))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
