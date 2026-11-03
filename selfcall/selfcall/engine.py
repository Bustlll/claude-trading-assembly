"""The self-calling engine.

A demand is a request for one Claude invocation. The engine runs it, stores the
answer, then reads the answer's next_demands and runs those too, but only if they
pass the policy below. The model proposes follow-ups; the engine decides.

Hard rules the model cannot change:
  * a demand's tools, permission rules and budget come from its KIND, never from the model
  * a kind may only spawn the kinds listed in its may_spawn
  * depth, run count, total cost, children per answer and prompt size are capped
  * a STOP file halts everything before the next invocation
  * every invocation's permission denials are recorded, so a blocked action is visible
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import time
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Callable

from .events import default_route

ROOT = Path(__file__).resolve().parent.parent

EFFORTS = ("low", "medium", "high", "xhigh", "max")

# model:  alias ("sonnet", "haiku", "opus") or full name; each kind picks its own
# effort: optional --effort level. Opus at "max" is refused by policy_check (energy cost).
# tools:  what the run can see (--tools)
# allow:  what runs without a prompt (--allowedTools); defaults to tools.
#         Patterns like "Bash(python3 /x/tool.py *)" narrow a tool to one command.
# notes:  extra contract text for this kind (e.g. where it may write)
KINDS = {
    "research": {"tools": ["WebSearch", "WebFetch"], "model": "sonnet", "effort": "high",
                 "budget_usd": 0.60, "may_spawn": ["research", "reflect"]},
    "work": {"tools": ["Read", "Glob", "Grep"], "model": "sonnet", "effort": "high",
             "budget_usd": 0.40, "may_spawn": ["work", "reflect"]},
    "reflect": {"tools": [], "model": "haiku", "effort": "low", "budget_usd": 0.15, "may_spawn": []},
}

LIMITS = {
    "max_depth": 3,              # root demand is depth 0
    "max_runs": 40,              # invocations ever made by this home dir
    "max_total_cost_usd": 8.0,   # sum of total_cost_usd over all runs in this home dir
    "max_children": 4,           # follow-ups accepted from one answer
    "max_prompt_chars": 6000,
    "timeout_s": 900,
    "max_context_chars": 3000,
}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "blocked"]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {"type": "string"}},
        "sources": {"type": "array", "items": {"type": "string"}},
        "next_demands": {"type": "array", "items": {
            "type": "object",
            "properties": {"kind": {"type": "string"}, "prompt": {"type": "string"}},
            "required": ["kind", "prompt"],
        }},
    },
    "required": ["status", "summary", "findings", "sources", "next_demands"],
}

COLUMNS = ("id", "kind", "prompt", "context", "parent", "depth", "source", "status",
           "created", "started", "finished", "answer", "error", "cost_usd", "session_id",
           "links", "phash", "next_try")

# Words that mean the subscription or the API is refusing for now (usage window, rate, overload).
LIMIT_MARKERS = ("usage limit", "session limit", "rate limit", "limit reached", "too many requests",
                 "429", "overloaded", "try again later", "credit balance", "quota", "hit your")
# 2026-11-02: the account itself can't be used -- billing lapsed, subscription switched off, login gone (seen on a
# closed subscription: "can't use your Claude subscription ... payment is past due", then "Your organization has disabled
# Claude subscription access for Claude Code"; a revoked token: "Not logged in · Please run /login"). Raised as LimitHit
# with an "account down:" prefix, so an account failover moves the call to another account (and keeps this one out for
# hours), and a single-account run waits and retries instead of failing the demand.
ACCOUNT_DOWN_MARKERS = ("can't use your claude subscription", "payment is past due", "disabled claude subscription access",
                        "not logged in", "please run /login", "oauth token has expired", "oauth token has been revoked",
                        "invalid bearer token", "authentication_error")
BACKOFF_BASE_S = 900           # first guessed wait after a limit with no stated reset time: 15 minutes
BACKOFF_MAX_S = 3600           # a guessed wait never exceeds an hour between retries
PARSED_RESET_BUFFER_S = 60     # add a little after a stated reset time, so the retry lands just past it
PARSED_RESET_MAX_S = 8 * 24 * 3600  # never trust a parsed reset more than 8 days out (a bad parse, not real)

_WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3,
            "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
# an optional day name before the time: the session limit says "resets 6:50am (tz)", the weekly one is
# expected to say "resets Thursday 10:00pm (tz)" -- same pattern, one extra word.
_RESET_RE = __import__("re").compile(
    r"resets?\s+(?:([A-Za-z]+)\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*\(([^)]+)\)", __import__("re").I)


def parse_reset_wait(text: str, now=None) -> float | None:
    """Parse a stated reset time into seconds to wait. Handles the session form, "resets 6:50am (tz)",
    and the weekly form, "resets Thursday 10:00pm (tz)" (next occurrence of that weekday). None when the
    message has no such time, its timezone name is not recognisable, or a named day isn't a real weekday."""
    import datetime as _dt
    m = _RESET_RE.search(text)
    if not m:
        return None
    day_name, hour, minute, ampm, tzname = m.groups()
    hour = int(hour) % 12 + (12 if ampm.lower() == "pm" else 0)
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        return None
    now = (now or _dt.datetime.now(tz)).astimezone(tz)
    target = now.replace(hour=hour, minute=int(minute or 0), second=0, microsecond=0)
    if day_name:
        weekday = _WEEKDAYS.get(day_name.lower())
        if weekday is None:
            return None
        target += _dt.timedelta(days=(weekday - target.weekday()) % 7)
        if target <= now:
            target += _dt.timedelta(days=7)
    elif target <= now:
        target += _dt.timedelta(days=1)
    return (target - now).total_seconds()


class LimitHit(RuntimeError):
    """The call was refused for a limit, not for a bad answer. The demand waits and is retried."""


def policy_check(name: str, pol: dict) -> None:
    """Refuse a kind whose model/effort pair is not allowed. Runs at Engine start and
    again at every call, so no kind table can smuggle in Opus at max effort."""
    effort = pol.get("effort")
    if effort is not None and effort not in EFFORTS:
        raise ValueError(f"kind {name!r}: effort {effort!r} not in {EFFORTS}")
    if "opus" in str(pol["model"]).lower() and effort == "max":
        raise ValueError(f"kind {name!r}: opus at max effort is refused (it wastes too much energy)")


def contract(kind: str, pol: dict, limits: dict) -> str:
    kids = ", ".join(pol["may_spawn"]) or "none (you may not spawn follow-ups)"
    tools = ", ".join(pol["tools"]) or "none"
    text = (
        "You are one invocation inside a self-calling program. You answer exactly one DEMAND.\n"
        "The program stores your answer, enforces every limit, and decides whether any follow-up runs.\n"
        "- Do the work the demand asks, using only your tools. External facts need a URL in sources.\n"
        "- status is \"blocked\" if you cannot do it with the tools you have; say why in summary.\n"
        f"- next_demands: only follow-ups REQUIRED to finish the goal, at most {limits['max_children']}. "
        f"kind must be one of: {kids}. Each prompt must be self-contained: the child sees only what you write. "
        "Leave it empty when the demand is finished.\n"
        f"- Your tools this run: {tools}."
    )
    if pol.get("notes"):
        text += "\n" + pol["notes"]
    return text


def build_prompt(demand: dict) -> str:
    parts = [f"DEMAND ({demand['kind']}):\n{demand['prompt']}"]
    if demand.get("context"):
        parts.append("CONTEXT FROM THE PARENT RUN (supplied by the program):\n" + demand["context"])
    return "\n\n".join(parts)


def billing_guard() -> None:
    """Refuse to run when an API key would bill the API instead of the local subscription.

    Calls inherit this environment. A set ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN makes `claude -p`
    use the API account. Set SELFCALL_ALLOW_API_BILLING=1 to allow that on purpose.
    """
    keys = [k for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN") if os.environ.get(k)]
    if keys and os.environ.get("SELFCALL_ALLOW_API_BILLING") != "1":
        raise RuntimeError(f"{'/'.join(keys)} is set, so calls would bill the API, not the subscription. "
                           "Unset it, or set SELFCALL_ALLOW_API_BILLING=1 to allow it on purpose.")


def call_claude(demand: dict, pol: dict, cmd: list[str], workspace: Path, limits: dict) -> dict:
    """One real Claude invocation. Raises RuntimeError on any failure."""
    billing_guard()
    policy_check(demand["kind"], pol)
    tools = ",".join(pol["tools"])
    allow = ",".join(pol.get("allow", pol["tools"]))
    argv = [*cmd, "-p", "--output-format", "json",
            "--model", pol["model"],
            "--max-budget-usd", str(pol["budget_usd"]),
            "--tools", tools,
            "--permission-mode", "dontAsk",
            "--append-system-prompt", contract(demand["kind"], pol, limits),
            "--json-schema", json.dumps(OUTPUT_SCHEMA)]
    if pol.get("effort"):
        argv += ["--effort", pol["effort"]]
    if allow:
        argv += ["--allowedTools", allow]
    if os.environ.get("SELFCALL_ALLOW_MCP") != "1":
        # 2026-10-18 (ftmo_blank, measured): without this every call loads the account's claude.ai MCP
        # connectors, whose instructions arrive AFTER the first turn -- the system prompt changes between turn 1
        # and turn 2, so turn 2 cannot reuse turn 1's prompt cache and re-writes the whole prompt (a scout paid
        # its prompt twice). The connector text (2.1k characters of "make a doc" instructions) is also nothing a
        # worker can use: its tools are --tools/--allowedTools above. Probe: a 2-turn call cost $0.048 without the
        # flag, $0.003 with it (turn 2: 13.0k tokens re-written vs 161). SELFCALL_ALLOW_MCP=1 restores the old way.
        argv += ["--strict-mcp-config"]
    # 2026-10-19 (ftmo_blank consult.py): the call's own demand id rides in the environment, so a tool the model runs
    # can tie what it does back to the turn that asked (a specialist consulted mid-turn is recorded as its child)
    env = {**os.environ, "SELFCALL_DEMAND_ID": str(demand.get("id") or ""), "SELFCALL_DEMAND_KIND": str(demand["kind"])}
    proc = subprocess.run(argv, input=build_prompt(demand), capture_output=True, text=True,
                          cwd=workspace, timeout=limits["timeout_s"], env=env)
    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError:
        text = (proc.stdout + proc.stderr).lower()
        if any(m in text for m in LIMIT_MARKERS):
            raise LimitHit(f"limit (no envelope): {(proc.stdout + proc.stderr)[-300:]}")
        if any(m in text for m in ACCOUNT_DOWN_MARKERS):
            raise LimitHit(f"account down (no envelope): {(proc.stdout + proc.stderr)[-300:]}")
        raise RuntimeError(f"no JSON envelope (rc={proc.returncode}): {proc.stderr[-400:]}")
    denials = env.get("permission_denials") or []
    if env.get("is_error") or env.get("subtype") != "success":
        result_text = str(env.get("result") or "")
        if any(m in result_text.lower() for m in LIMIT_MARKERS):
            raise LimitHit(f"limit: {result_text[:400]}")
        if any(m in result_text.lower() for m in ACCOUNT_DOWN_MARKERS):
            raise LimitHit(f"account down: {result_text[:400]}")
        raise RuntimeError(f"claude {env.get('subtype')}: {result_text[:400]}")
    ans = env.get("structured_output")
    if (not isinstance(ans, dict) or ans.get("status") not in ("done", "blocked")
            or not isinstance(ans.get("next_demands", []), list)):
        raise RuntimeError("structured_output missing or malformed")
    return {"answer": ans, "cost_usd": float(env.get("total_cost_usd") or 0),
            "session_id": env.get("session_id"), "denials": denials}


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS demands (id TEXT PRIMARY KEY, kind TEXT, prompt TEXT, "
            "context TEXT, parent TEXT, depth INTEGER, source TEXT, status TEXT, created REAL, "
            "started REAL, finished REAL, answer TEXT, error TEXT, cost_usd REAL DEFAULT 0, "
            "session_id TEXT, links TEXT, phash TEXT)")
        try:
            self.db.execute("ALTER TABLE demands ADD COLUMN next_try REAL")
        except sqlite3.OperationalError:
            pass  # column already exists
        self.db.commit()

    def add(self, kind, prompt, context, parent, depth, source, dedupe) -> str | None:
        phash = hashlib.sha256(f"{kind}\n{prompt}".encode()).hexdigest()
        if dedupe and self.db.execute("SELECT 1 FROM demands WHERE phash=?", (phash,)).fetchone():
            return None
        did = os.urandom(6).hex()
        self.db.execute(
            "INSERT INTO demands (id, kind, prompt, context, parent, depth, source, status, created, phash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (did, kind, prompt, context, parent, depth, source, "queued", time.time(), phash))
        self.db.commit()
        return did

    def next_queued(self):
        return self.db.execute(
            "SELECT * FROM demands WHERE status='queued' AND (next_try IS NULL OR next_try <= ?) "
            "ORDER BY depth, created LIMIT 1", (time.time(),)).fetchone()

    def waiting_for_limit(self):
        """Earliest retry time among demands that are queued but waiting out a limit, or None."""
        row = self.db.execute("SELECT MIN(next_try) AS t FROM demands WHERE status='queued' "
                              "AND next_try IS NOT NULL AND next_try > ?", (time.time(),)).fetchone()
        return row["t"]

    def get(self, did):
        return self.db.execute("SELECT * FROM demands WHERE id=?", (did,)).fetchone()

    def set(self, did, **fields):
        assert set(fields) <= set(COLUMNS), fields
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE demands SET {cols} WHERE id=?", (*fields.values(), did))
        self.db.commit()

    def totals(self):
        return self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost_usd),0) FROM demands WHERE started IS NOT NULL").fetchone()

    def recover_interrupted(self):
        self.db.execute("UPDATE demands SET status='failed', error='interrupted: worker died mid-call' "
                        "WHERE status='running'")
        self.db.commit()

    def recent(self, n=10):
        return self.db.execute("SELECT * FROM demands ORDER BY created DESC LIMIT ?", (n,)).fetchall()

    def counts(self):
        return {r["status"]: r["c"] for r in self.db.execute(
            "SELECT status, COUNT(*) AS c FROM demands GROUP BY status")}


class Engine:
    def __init__(self, home: Path = ROOT, claude_cmd: list[str] | None = None, invoker=None,
                 kinds: dict | None = None, router: Callable | None = None, limits: dict | None = None):
        self.home = Path(home)
        self.state = self.home / "state"
        self.runs = self.home / "runs"
        self.workspace = self.home / "workspace"
        for p in (self.state, self.runs, self.workspace):
            p.mkdir(parents=True, exist_ok=True)
        self.kinds = kinds or KINDS
        for name, pol in self.kinds.items():
            policy_check(name, pol)
        self.limits = {**LIMITS, **(limits or {})}
        self.router = router or default_route
        self.stop_file = self.state / "STOP"
        self.store = Store(self.state / "selfcall.db")
        self.limit_streak = 0
        cmd = claude_cmd or shlex.split(os.environ.get("SELFCALL_CLAUDE_BIN", "claude"))
        # child_view(kind, prompt, parent_row) -> text or None (2026-10-18): what the PROGRAM attaches to a child's
        # context at spawn time, beyond the parent's own summary -- the host sets it (ftmo_blank: a symbol trader
        # handed a setup gets its symbol file). None = children see only what the parent wrote, as before.
        self.child_view = None
        self.invoker = invoker or (lambda d: call_claude(d, self.kinds[d["kind"]], cmd,
                                                         self.workspace, self.limits))

    # ---- admission -------------------------------------------------------
    def enqueue(self, kind: str, prompt: str, source: str = "cli") -> str:
        if kind not in self.kinds:
            raise ValueError(f"unknown kind {kind!r}; known: {sorted(self.kinds)}")
        prompt = prompt.strip()
        if not prompt or len(prompt) > self.limits["max_prompt_chars"]:
            raise ValueError("prompt empty or too long")
        did = self.store.add(kind, prompt, "", None, 0, source, dedupe=False)
        assert did is not None
        return did

    def emit(self, event: str, payload: dict) -> str | None:
        """Route an event to a demand. The router decides kind and prompt; returning
        None means the event is recorded but does not wake the model."""
        routed = self.router(event, payload)
        if routed is None:
            return None
        kind, prompt = routed
        return self.enqueue(kind, prompt, source=f"event:{event}")

    def halt_reason(self) -> str | None:
        if self.stop_file.exists():
            return "STOP file present (run `python -m selfcall resume` to clear)"
        n, cost = self.store.totals()
        if n >= self.limits["max_runs"]:
            return f"max_runs reached ({n})"
        if cost >= self.limits["max_total_cost_usd"]:
            return f"cost cap reached (${cost:.2f})"
        return None

    # ---- one step --------------------------------------------------------
    def step(self):
        """Run at most one demand. Returns (what, detail)."""
        why = self.halt_reason()
        if why:
            return "halted", why
        row = self.store.next_queued()
        if row is None:
            return "idle", None
        d = dict(row)
        self.store.set(d["id"], status="running", started=time.time())
        try:
            res = self.invoker(d)
        except LimitHit as exc:  # not a failure of the demand: wait, then retry the same demand
            stated = parse_reset_wait(str(exc))
            if stated is not None:
                wait = min(PARSED_RESET_MAX_S, max(0.0, stated)) + PARSED_RESET_BUFFER_S
                how = f"until the stated reset (+{PARSED_RESET_BUFFER_S}s)"
            else:
                self.limit_streak += 1
                wait = min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** (self.limit_streak - 1))
                how = "guessed backoff, no reset time in the message"
            self.store.set(d["id"], status="queued", started=None, next_try=time.time() + wait,
                           error=f"waiting for a limit ({int(wait)}s, {how}): {str(exc)[:300]}")
            return "limited", time.time() + wait
        except Exception as exc:  # any failure is recorded, never silently dropped
            self.store.set(d["id"], status="failed", error=str(exc)[:2000], finished=time.time())
            self._write_record(d["id"])
            return "failed", d["id"]
        self.limit_streak = 0
        ans = res["answer"]
        self.store.set(d["id"], status=ans["status"], answer=json.dumps(ans),
                       cost_usd=res["cost_usd"], session_id=res["session_id"], finished=time.time())
        links = {"spawned": [], "rejected": [], "denials": res.get("denials", [])}
        if ans["status"] == "done":
            links.update(self._spawn(self.store.get(d["id"]), ans.get("next_demands", [])))
        self.store.set(d["id"], links=json.dumps(links))
        self._write_record(d["id"])
        return "ran", d["id"]

    def _context_from(self, parent_row) -> str:
        ans = json.loads(parent_row["answer"] or "{}")
        text = f"Parent summary: {ans.get('summary', '')}\nParent findings:\n" + \
               "\n".join(f"- {f}" for f in ans.get("findings", []))
        return text[:self.limits["max_context_chars"]]

    def _spawn(self, parent_row, proposals) -> dict:
        allowed = self.kinds[parent_row["kind"]]["may_spawn"]
        ctx = self._context_from(parent_row)
        out = {"spawned": [], "rejected": []}
        for i, p in enumerate(proposals):
            kind = str(p.get("kind", "")).strip()
            prompt = str(p.get("prompt", "")).strip()
            tag = f"{kind}: {prompt[:60]}"
            if i >= self.limits["max_children"]:
                why = "over max_children"
            elif kind not in allowed:
                why = f"{parent_row['kind']} may not spawn {kind}"
            elif parent_row["depth"] + 1 > self.limits["max_depth"]:
                why = "max_depth"
            elif not prompt or len(prompt) > self.limits["max_prompt_chars"]:
                why = "empty or too long"
            else:
                cctx = ctx
                if self.child_view is not None:
                    try:
                        extra = self.child_view(kind, prompt, parent_row)
                    except Exception as exc:  # a view that cannot be built never blocks the child itself
                        extra = f"(the program could not build this child's view: {type(exc).__name__}: {exc})"
                    if extra:
                        cctx = (ctx + "\n\n" if ctx else "") + extra
                cid = self.store.add(kind, prompt, cctx, parent_row["id"], parent_row["depth"] + 1,
                                     "child", dedupe=True)
                if cid:
                    out["spawned"].append(cid)
                    continue
                why = "duplicate of an earlier demand"
            out["rejected"].append({"demand": tag, "why": why})
        return out

    def _write_record(self, did: str):
        row = self.store.get(did)
        rec = {k: row[k] for k in ("id", "kind", "status", "depth", "parent", "source", "prompt",
                                   "cost_usd", "session_id", "error", "created", "started", "finished")}
        rec["answer"] = json.loads(row["answer"]) if row["answer"] else None
        rec["links"] = json.loads(row["links"]) if row["links"] else {}
        (self.runs / f"{did}.json").write_text(json.dumps(rec, indent=2))
        (self.runs / f"{did}.md").write_text(render_md(rec))

    # ---- loops -----------------------------------------------------------
    def drain(self, max_steps: int = 1000):
        """Run until the queue is empty, the engine halts, or a limit makes the next demand wait."""
        self.store.recover_interrupted()
        for _ in range(max_steps):
            what, detail = self.step()
            if what in ("idle", "halted", "limited"):
                return what, detail
        return "max_steps", max_steps

    def serve(self, poll_s: float = 5.0):
        """Run forever: work when there is work, sleep when idle or halted."""
        self.store.recover_interrupted()
        while True:
            what, _ = self.step()
            if what in ("idle", "halted"):
                time.sleep(poll_s)


def render_md(rec: dict) -> str:
    ans = rec["answer"] or {}
    lines = [f"# {rec['kind']} {rec['id']}  [{rec['status']}]",
             f"- depth {rec['depth']} · parent {rec['parent'] or '-'} · source {rec['source']} · "
             f"cost ${rec['cost_usd'] or 0:.4f}",
             "", "## Demand", rec["prompt"], "", "## Summary", ans.get("summary", rec["error"] or "")]
    if ans.get("findings"):
        lines += ["", "## Findings"] + [f"- {f}" for f in ans["findings"]]
    if ans.get("sources"):
        lines += ["", "## Sources"] + [f"- {s}" for s in ans["sources"]]
    links = rec.get("links") or {}
    if links.get("spawned"):
        lines += ["", "## Spawned"] + [f"- {s}" for s in links["spawned"]]
    if links.get("rejected"):
        lines += ["", "## Rejected follow-ups"] + [f"- {r['demand']} — {r['why']}" for r in links["rejected"]]
    if links.get("denials"):
        lines += ["", "## Permission denials"] + [f"- {json.dumps(x)[:300]}" for x in links["denials"]]
    return "\n".join(lines) + "\n"


def single_worker(home: Path):
    """Exclusive lock so only one process steps the queue at a time."""
    lock = open(Path(home) / "state" / "worker.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if another worker holds it
    return lock
