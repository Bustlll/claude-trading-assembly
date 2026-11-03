"""Stand-in for the `claude` binary so the engine can be tested without tokens.

It reads the prompt from stdin like `claude -p`, logs its argv to $SELFCALL_FAKE_LOG,
and answers with the same JSON envelope shape the real CLI prints. Behaviour is
chosen by markers in the prompt:
  SPAWN      -> proposes one reflect child (unless the prompt is itself a CHILD)
  SPAWNWORK  -> proposes one work child instead
  BADKIND    -> proposes a research child (a work parent may not spawn it)
  BLOCK      -> status "blocked"
  FAILENV    -> an error envelope (is_error true)
  NOJSON     -> prints non-JSON
"""
import json
import os
import sys

prompt = sys.stdin.read()
argv = sys.argv[1:]
log = os.environ.get("SELFCALL_FAKE_LOG")
if log:
    try:  # a stale log path from an earlier test must not stop the fake from answering
        with open(log, "a") as f:
            f.write(json.dumps(argv) + "\n")
    except OSError:
        pass

if "NOJSON" in prompt:
    print("this is not json")
    sys.exit(0)

if "ACCOUNTDOWN" in prompt:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": "Your organization has disabled Claude subscription access for Claude Code", "total_cost_usd": 0}))
    sys.exit(0)

if "SESSIONLIMIT" in prompt:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "result": "You've hit your session limit \u00b7 resets 11:59pm (UTC)", "total_cost_usd": 0}))
    sys.exit(0)

if "LIMIT" in prompt:
    print(json.dumps({"type": "result", "subtype": "error", "is_error": True,
                      "result": "Claude usage limit reached. Your limit resets later.", "total_cost_usd": 0}))
    sys.exit(0)

if "FAILENV" in prompt:
    print(json.dumps({"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
                      "result": "budget exceeded", "total_cost_usd": 0.6}))
    sys.exit(0)

status = "blocked" if "BLOCK" in prompt else "done"
nxt = []
if "SPAWNWORK" in prompt and "CHILD" not in prompt:
    nxt.append({"kind": "work", "prompt": "CHILD read the notes and report"})
elif "SPAWN" in prompt and "CHILD" not in prompt:
    nxt.append({"kind": "reflect", "prompt": "CHILD summarise the notes"})
if "BADKIND" in prompt:
    nxt.append({"kind": "research", "prompt": "a research child from a work parent"})

answer = {"status": status, "summary": f"fake answer to: {prompt[:40]!r}",
          "findings": ["f1"], "sources": [], "next_demands": nxt}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": "fake-session", "total_cost_usd": 0.01,
                  "result": json.dumps(answer), "structured_output": answer}))
