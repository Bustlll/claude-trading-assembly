"""MONITOR UI: a read-only localhost page showing what the FTMO field agent is doing.

  python3 tools/monitor_ui.py [--port 8765]        then open http://127.0.0.1:8765

Reads state/ and runs/ only. Never writes, never sends an order. Refreshes every 3 seconds.
"""
import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME))
sys.path.insert(0, str(HOME / "tools"))
sys.path.insert(0, str(HOME.parent / "selfcall"))

from selfcall.engine import Engine  # noqa: E402
from guard import policy  # noqa: E402
import paper as P  # noqa: E402
import live_broker as LB  # noqa: E402
import live_loop as LL  # noqa: E402  (window + next-decision helpers only; nothing here ticks the loop)

FILL_TYPES = {"order_placed", "trade_opened", "trade_closed", "order_rejected", "stop_moved", "close_placed",
              "rule_breach", "risk_warning", "goal_passed", "replay_end"}

# Loading every symbol's candles (one of them now 1.9M M5 bars) took ~7s and was being repeated on every
# single poll (every 3s), by this server AND by the live driver process at the same time -- requests piled
# up faster than they could be served (one real request measured at 50s). The timeline and the windows are
# the only pieces of a Paper() that a status page actually needs, and they are cheap once computed; paying
# the full load ONCE at startup and reusing it is what fixes that, not loading it again on every request.
print("loading symbol timeline once at startup (this is the only slow step)...", flush=True)
_t0 = time.time()
_boot = P.Paper()
_TIMELINE = _boot.timeline
_WINDOWS = _boot.windows
_N_SYMBOLS = len(_boot.feeds)
del _boot  # release the actual candle data; only the three cheap things above are kept
print(f"...done in {time.time() - _t0:.1f}s. Every poll after this reads state/account.json directly.", flush=True)

_FRESH_STATE_DEFAULTS = {"cursor": 0, "balance": P.R.START_BALANCE, "equity": P.R.START_BALANCE, "day": None,
                         "day_start_equity": P.R.START_BALANCE, "traded_days": [], "positions": [], "pending": [],
                         "breached": None, "field_read": False, "note_days": [], "last_close": {}, "attempt": 1,
                         "epoch": 0, "window_index": 0, "attempts_total": 0, "attempts_passed": 0, "start_day": None}


def fast_snapshot_and_goals(home=HOME):
    """The same numbers Paper.snapshot()/goals() would give, read straight from account.json: no feeds,
    no timeline recomputation, no per-request Paper() construction."""
    path = Path(home) / "state" / "account.json"
    s = json.loads(path.read_text()) if path.exists() else dict(_FRESH_STATE_DEFAULTS)
    c = s["cursor"]
    eq = s["equity"]
    day_used = s["day_start_equity"] - eq
    total_used = P.R.START_BALANCE - eq
    open_risk = sum(P.R.risk_usd(p["symbol"], p["lots"], p["entry"], p["sl"]) for p in s["positions"]
                    if p.get("sl") is not None)
    utc = (P.bar_utc(_TIMELINE[c - 1]).strftime("%Y-%m-%d %H:%M UTC") if 0 < c <= len(_TIMELINE) else "not started")
    rate = (s["attempts_passed"] / s["attempts_total"]) if s["attempts_total"] else None
    window_day = P.business_days_between(s["start_day"], s["day"]) if s.get("start_day") and s.get("day") else 0

    def half(sym):
        return P.R.spec(sym)["spread_price_snapshot"] / 2

    def floating_usd(p):
        mark = s["last_close"].get(p["symbol"], p["entry"])
        exit_px = mark - half(p["symbol"]) if p["side"] == "buy" else mark + half(p["symbol"])
        d = (exit_px - p["entry"]) if p["side"] == "buy" else (p["entry"] - exit_px)
        return round(d * p["lots"] * P.R.spec(p["symbol"])["usd_per_price_unit_per_lot"], 2)

    positions = [{**p, "floating_usd": floating_usd(p)} for p in s["positions"]]
    tail = tail_jsonl(Path(home) / "state" / "ledger.jsonl", 20_000)
    attempt_trades = [r for r in tail if r.get("type") == "trade_closed" and r.get("attempt", 1) == s["attempt"]]
    trades_today = [t for t in attempt_trades if (t.get("closed_utc") or "")[:10] == s.get("day")]
    by_day: dict = {}
    for t in attempt_trades:
        d = (t.get("closed_utc") or "")[:10]
        by_day[d] = by_day.get(d, 0) + 1
    recent_attempts = [r for r in tail if r.get("type") == "attempt_ended"][-8:]
    snap = {
        "utc": utc, "bar_index": c, "bars_total": len(_TIMELINE), "equity": round(eq, 2),
        "balance": round(s["balance"], 2), "day_pnl": round(eq - s["day_start_equity"], 2),
        "daily_loss_used_usd": round(day_used, 2), "daily_loss_limit_usd": round(P.R.daily_loss_limit_usd(), 2),
        "total_loss_used_usd": round(total_used, 2), "total_loss_limit_usd": round(P.R.total_loss_limit_usd(), 2),
        "open_risk_usd": round(open_risk, 2), "floating_pnl_usd": round(sum(p["floating_usd"] for p in positions), 2),
        "positions": positions, "pending": s["pending"],
        "traded_days": len(s["traded_days"]), "rungs_passed": P.R.rungs_passed(eq), "breached": s["breached"],
        "attempt": s["attempt"], "epoch": s["epoch"], "window_index": s["window_index"],
        "windows_per_epoch": len(_WINDOWS), "window_day": window_day,
        "attempts_total": s["attempts_total"], "attempts_passed": s["attempts_passed"],
        "pass_rate": round(rate, 4) if rate is not None else None,
        "trades_closed_today": len(trades_today), "trades_closed_this_attempt": len(attempt_trades),
        "trades_by_day_this_attempt": dict(sorted(by_day.items())), "recent_attempts": recent_attempts,
    }
    goal_state = {"equity": eq, "traded_days": s["traded_days"], "note_days": s.get("note_days", []),
                  "field_read": s["field_read"], "breached": s["breached"], "window_day": window_day}
    return snap, P.R.goal_table(goal_state)


def driver_running() -> bool:
    """True only for a `driver.py run` whose working directory is this home (not scratch copies)."""
    try:
        out = subprocess.run(["pgrep", "-f", "driver.py run"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return False
    me = os.getpid()
    for pid in out.split():
        if int(pid) == me:
            continue
        try:
            if Path(os.readlink(f"/proc/{pid}/cwd")) == HOME:
                return True
        except OSError:
            continue
    return False


def tail_jsonl(path: Path, n: int) -> list:
    if not path.exists():
        return []
    with open(path) as f:
        lines = f.readlines()[-n:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def demands(eng: Engine, limit: int) -> list[dict]:
    rows = []
    for r in eng.store.recent(limit):
        rec = {}
        p = HOME / "runs" / f"{r['id']}.json"
        if p.exists():
            rec = json.loads(p.read_text())
        ans = rec.get("answer") or {}
        den = (rec.get("links") or {}).get("denials") or []
        rows.append({
            "id": r["id"], "kind": r["kind"], "status": r["status"], "source": r["source"],
            "created": r["created"], "started": r["started"], "finished": r["finished"],
            "cost": round(r["cost_usd"] or 0, 4), "denials": len(den),
            "summary": (ans.get("summary") or r["error"] or "")[:500],
            "prompt_head": (r["prompt"] or "").split("\n", 1)[0][:140],
        })
    return rows


def waiting_for(eng: Engine, snap: dict, running: bool, queued: list, active: list) -> str:
    if active:
        a = active[0]
        return f"model call running: {a['kind']} ({a['source']})"
    if queued:
        return f"{len(queued)} demand(s) queued, next: {queued[0]['kind']} ({queued[0]['source']})"
    why = eng.halt_reason()
    if why:
        return f"engine halted: {why}"
    if not running:
        return "idle: no driver loop is running (start it with driver.py run)"
    return "between turns: the next replay turn starts after the current loop step"


def status() -> dict:
    eng = Engine(home=HOME, kinds=policy.KINDS, limits=policy.LIMITS)
    snap, goals = fast_snapshot_and_goals()
    counts = eng.store.counts()
    n, cost = eng.store.totals()
    recent = demands(eng, 40)
    active = [d for d in recent if d["status"] == "running"]
    queued = [d for d in recent if d["status"] == "queued"]
    running = driver_running()
    turns = tail_jsonl(HOME / "state" / "turns.jsonl", 12)
    ledger = [r for r in tail_jsonl(HOME / "state" / "ledger.jsonl", 400) if r.get("type") in FILL_TYPES][-20:]
    return {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "driver_running": running,
        "control": control_state(),
        "waiting_for": waiting_for(eng, snap, running, queued, active),
        "account": {k: snap[k] for k in ("utc", "bar_index", "bars_total", "equity", "balance", "day_pnl",
                                         "daily_loss_used_usd", "daily_loss_limit_usd", "total_loss_used_usd",
                                         "total_loss_limit_usd", "open_risk_usd", "floating_pnl_usd",
                                         "positions", "pending", "traded_days", "rungs_passed", "breached",
                                         "attempt", "epoch", "window_index", "windows_per_epoch", "window_day",
                                         "attempts_total", "attempts_passed", "pass_rate", "trades_closed_today",
                                         "trades_closed_this_attempt", "trades_by_day_this_attempt")},
        "goals": goals,
        "recent_attempts": snap["recent_attempts"],
        "engine": {"counts": counts, "invocations": n, "cost_usd": round(cost, 4),
                   "cost_cap": LIMITS_CAP(), "run_cap": (policy.LIMITS["max_runs"] if policy.LIMITS["max_runs"] < 10**8 else None), "halt": eng.halt_reason() or "none"},
        "queue": {"active": active, "queued": queued},
        "recent_demands": recent,
        "turns": turns,
        "fills": ledger,
    }


def LIMITS_CAP():
    """None when there is no real cap (2026-10-11: training runs on the subscription, no cost cap)."""
    cap = policy.LIMITS["max_total_cost_usd"]
    return cap if cap < 1e8 else None


WORKER_KINDS = ["scout", "chart", "timing", "review", "trade", "manage"] + list(getattr(policy, "SYMBOL_TRADER_KINDS", []))


def consulted_breakdown(home=HOME, n=5000) -> dict:
    """Every closed trade, split by whether paper.py found a pre_trade board post before it was
    opened (see tools/paper.py _consulted_analysis) -- this is what lets the operator (and a later
    review) see whether winning or losing trades tend to come WITH or WITHOUT prior specialist
    analysis, using the broker's own verified flag, not anyone's self-report."""
    rows = [r for r in tail_jsonl(Path(home) / "state" / "ledger.jsonl", n) if r.get("type") == "trade_closed"]

    def agg(subset):
        n = len(subset)
        wins = sum(1 for r in subset if (r.get("net_usd") or 0) > 0)
        return {"n": n, "win_rate": round(wins / n, 3) if n else None,
                "net_usd": round(sum(r.get("net_usd") or 0 for r in subset), 2)}

    by_symbol: dict = {}
    for r in rows:
        bucket = by_symbol.setdefault(r.get("symbol", "?"), {"with": [], "without": []})
        bucket["with" if r.get("consulted_analysis") else "without"].append(r)
    withs = [r for r in rows if r.get("consulted_analysis")]
    withouts = [r for r in rows if not r.get("consulted_analysis")]
    return {"overall": {"with_analysis": agg(withs), "without_analysis": agg(withouts)},
            "by_symbol": {s: {"with_analysis": agg(v["with"]), "without_analysis": agg(v["without"])}
                          for s, v in sorted(by_symbol.items())},
            "tracked_since": "2026-10-08 (trades closed before this do not carry the flag; they count as without)"}


def board_status() -> dict:
    eng = Engine(home=HOME, kinds=policy.KINDS, limits=policy.LIMITS)
    posts = list(reversed(tail_jsonl(HOME / "state" / "board.jsonl", 200)))
    recent = demands(eng, 250)
    workers = {}
    for k in WORKER_KINDS:
        rows = [r for r in recent if r["kind"] == k]
        workers[k] = {"invocations": len(rows), "cost_usd": round(sum(r["cost"] for r in rows), 4),
                      "may_spawn": policy.KINDS.get(k, {}).get("may_spawn", []),
                      "recent": rows[:6]}
    return {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "posts": posts, "workers": workers,
            "consulted": consulted_breakdown(), "driver_running": driver_running()}


# ── LIVE page (2026-10-12, operator: "a live submenu that displays all needed to see") ──────────────
# Everything the live assembly does, from files it already writes -- no extra MT5 call: the account
# numbers are the snapshot live_loop publishes to state/market_feed.json every M5 tick. The one new
# source is each Claude call's own transcript (claude -p keeps it under <config dir>/projects/<workspace
# slug>/<session>.jsonl, written as the call runs), so a RUNNING call shows the commands it is making.
LIVE_DB = HOME / "state" / "live_selfcall.db"
_SESSION_SLUG = re.sub(r"[^A-Za-z0-9]", "-", str(HOME / "workspace"))
_session_cache: dict = {}   # demand id -> its transcript path, once found
_unit_cache: dict = {}


def _session_dirs() -> list:
    """Every castl account's transcript folder for this workspace (~/.claude, ~/.claude-a, ~/.claude-b)."""
    return [p for p in Path.home().glob(f".claude*/projects/{_SESSION_SLUG}") if p.is_dir()]


def _first_user_text(path: Path) -> str:
    try:
        with open(path) as fh:
            for i, line in enumerate(fh):
                if i > 60:
                    break
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if r.get("type") != "user":
                    continue
                c = (r.get("message") or {}).get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    return "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    except OSError:
        pass
    return ""


def _transcript_path(d: dict):
    """A finished call is found by its session id. A RUNNING one has no session id yet (the engine
    records it at the end), so it is matched by its opening message: build_prompt() starts every call
    with 'DEMAND (<kind>):' followed by the demand's own prompt."""
    hit = _session_cache.get(d["id"])
    if hit and hit.exists():
        return hit
    dirs = _session_dirs()
    if d.get("session_id"):
        for base in dirs:
            f = base / f"{d['session_id']}.jsonl"
            if f.exists():
                _session_cache[d["id"]] = f
                return f
        return None
    if d.get("status") != "running" or not d.get("started"):
        return None
    head = f"DEMAND ({d['kind']}):\n{(d.get('prompt') or '')[:200]}"
    cands = []
    for base in dirs:
        for f in base.glob("*.jsonl"):
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            if mt >= d["started"] - 5:
                cands.append((mt, f))
    for _, f in sorted(cands, reverse=True)[:12]:
        if _first_user_text(f).startswith(head):
            _session_cache[d["id"]] = f
            return f
    return None


def transcript_steps(path, limit: int = 60) -> list:
    """What the worker did, in order: each tool call (the command it ran), each result (trimmed), any
    text it said, and its final answer -- read from the call's own transcript."""
    steps = []
    try:
        fh = open(path)
    except OSError:
        return steps
    with fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            c = (r.get("message") or {}).get("content")
            if not isinstance(c, list):
                continue
            ts = r.get("timestamp") or ""
            for b in c:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "tool_use":
                    inp = b.get("input") or {}
                    if b.get("name") == "StructuredOutput":
                        steps.append({"t": ts, "kind": "answer", "text": str(inp.get("summary", ""))[:800]})
                    else:
                        txt = inp.get("command") or inp.get("file_path") or inp.get("pattern") or json.dumps(inp)
                        steps.append({"t": ts, "kind": "tool", "tool": b.get("name"), "text": str(txt)[:800]})
                elif bt == "tool_result":
                    rc = b.get("content")
                    txt = rc if isinstance(rc, str) else " ".join(
                        x.get("text", "") for x in (rc or []) if isinstance(x, dict))
                    steps.append({"t": ts, "kind": "result", "text": txt[:500], "error": bool(b.get("is_error"))})
                elif bt == "text" and r.get("type") == "assistant" and b.get("text", "").strip():
                    steps.append({"t": ts, "kind": "say", "text": b["text"][:800]})
    return steps[-limit:]


def _unit_state(unit: str, ttl: float = 10.0) -> str:
    now = time.time()
    hit = _unit_cache.get(unit)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        val = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True,
                             timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        val = "unknown"
    _unit_cache[unit] = (now, val)
    return val


def _utc_epoch(s):
    """The live files stamp time three ways ('2026-10-12 07:38 UTC', '2026-10-12 07:38:49', ISO)."""
    if not s:
        return None
    s = str(s).replace(" UTC", "").replace("Z", "").split("+")[0].strip()
    if len(s) > 10 and s[10] == "T":   # ISO; only after ' UTC' is gone, or its T would be eaten too
        s = s[:10] + " " + s[11:]
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _jload(s, default):
    try:
        return json.loads(s) if s else default
    except (TypeError, json.JSONDecodeError):
        return default


def _live_rows(limit: int) -> list:
    if not LIVE_DB.exists():
        return []
    con = sqlite3.connect(LIVE_DB, timeout=2)
    try:
        rows = con.execute("SELECT id, kind, status, source, created, started, finished, cost_usd, error, "
                           "session_id, answer, prompt, links FROM demands ORDER BY created DESC LIMIT ?",
                           (limit,)).fetchall()
    finally:
        con.close()
    out = []
    for (i, kind, st, src, cr, sa, fi, cost, err, sid, ans, prompt, links) in rows:
        a = _jload(ans, {}) or {}
        out.append({"id": i, "kind": kind, "status": st, "source": src, "created": cr, "started": sa,
                    "finished": fi, "cost": round(cost or 0, 4), "error": err, "session_id": sid,
                    "secs": round(fi - sa, 1) if (fi and sa) else None,
                    "summary": (a.get("summary") or err or "")[:1200], "findings": a.get("findings") or [],
                    "next_demands": [n.get("kind") for n in (a.get("next_demands") or []) if isinstance(n, dict)],
                    "denials": len((_jload(links, {}) or {}).get("denials") or []),
                    "prompt": prompt or "", "prompt_head": (prompt or "").split("\n", 1)[0][:160]})
    return out


def _ledger_text(r: dict) -> str:
    t = r.get("type")
    if t == "order_placed":
        return (f"{r.get('side')} {r.get('lots')} {r.get('symbol')} @ {r.get('price') or r.get('entry') or ''} "
                f"sl {r.get('sl')} tp {r.get('tp')} ticket {r.get('ticket') or r.get('order') or '?'} -- {r.get('why') or ''}")
    if t == "order_refused":
        return (f"{r.get('side')} {r.get('lots')} {r.get('symbol')} REFUSED: "
                f"{r.get('reason') or r.get('comment') or r.get('error') or ''}" + (f" -- {r['why']}" if r.get("why") else ""))
    if t == "trade_closed":
        return (f"{r.get('symbol')} {r.get('side')} {r.get('result')} {r.get('net')} {r.get('currency') or ''} "
                f"({r.get('r_net')}R) {r.get('reason') or ''} -- opened {r.get('opened_utc')}, closed {r.get('closed_utc')}")
    if t == "journal":
        return str(r.get("text", ""))
    rest = {k: v for k, v in r.items() if k not in ("type", "source", "logged_utc", "utc", "day")}
    return json.dumps(rest, default=str)[:400]


def _turn_text(x: dict) -> str:
    parts = [f"{e.get('event')} -> {e.get('kind')}" for e in (x.get("events") or [])]
    parts += [f"skipped {s.get('event')}: {s.get('reason')}" for s in (x.get("skipped") or [])]
    if x.get("expired"):
        parts.append(f"{x['expired']} stale demand(s) expired")
    if x.get("error"):
        parts.append(f"ERROR {x['error']}")
    return "; ".join(parts) or (x.get("detail") or x.get("drain") or "tick")


def live_status() -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    prague0 = dt.datetime.now(ZoneInfo("Europe/Prague")).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    feed_file = _jload((HOME / "state" / "market_feed.json").read_text() if (HOME / "state" / "market_feed.json").exists() else "", {})
    snap = dict(feed_file.get("snapshot") or {}) if feed_file.get("mode") == "LIVE" else {}
    snap.pop("last_close", None)
    loop = _jload((HOME / "state" / "live_loop.json").read_text() if (HOME / "state" / "live_loop.json").exists() else "", {})
    turns = tail_jsonl(HOME / "state" / "live_turns.jsonl", 80)
    board = tail_jsonl(HOME / "state" / "live_board.jsonl", 150)
    ledger = tail_jsonl(HOME / "state" / "live_ledger.jsonl", 300)
    errors = tail_jsonl(HOME / "state" / "live_errors.jsonl", 30)
    calls = _live_rows(120)

    running = [d for d in calls if d["status"] == "running"]
    for d in running:
        p = _transcript_path(d)
        d["steps"] = transcript_steps(p, 40) if p else []
        d["elapsed"] = round(now.timestamp() - d["started"]) if d.get("started") else None
    for d in calls:
        d.pop("prompt", None)
    queued = [d for d in calls if d["status"] == "queued"]

    by_kind: dict = {}
    for k in policy.LIVE_KINDS:
        rows = [d for d in calls if d["kind"] == k]
        by_kind[k] = {"today": sum(1 for d in rows if (d.get("started") or 0) >= prague0), "recent": len(rows),
                      "cost": round(sum(d["cost"] for d in rows), 4),
                      "last": (rows[0]["summary"][:220] if rows else ""), "last_t": (rows[0].get("finished") or rows[0].get("created")) if rows else None}
    today = [d for d in calls if (d.get("started") or 0) >= prague0]

    feed = []
    for x in turns:
        feed.append({"t": _utc_epoch(x.get("at")), "src": "loop", "text": _turn_text(x)})
    for d in calls:
        if d["status"] == "queued":
            continue
        feed.append({"t": d.get("finished") or d.get("started") or d.get("created"), "src": "call", "id": d["id"],
                     "kind": d["kind"], "status": d["status"], "cost": d["cost"], "secs": d["secs"],
                     "source": d["source"], "next": d["next_demands"], "denials": d["denials"],
                     "text": d["summary"] if d["status"] != "running" else f"RUNNING ({d['source']})"})
    for p in board:
        feed.append({"t": _utc_epoch(p.get("utc")), "src": "board", "worker": p.get("worker"), "topic": p.get("topic"),
                     "symbol": p.get("symbol"), "text": p.get("text", "")})
    ledger_rows = []
    for i, r in enumerate(ledger):
        t = _utc_epoch(r.get("logged_utc") or r.get("utc") or r.get("closed_utc"))
        row = {"i": i, "t": t, "type": r.get("type"), "text": _ledger_text(r)}
        ledger_rows.append(row)
        if t is not None:
            feed.append({"src": "ledger", **row})
    for e in errors:
        feed.append({"t": e.get("ts_epoch") or _utc_epoch(e.get("ts")), "src": "error", "text": e.get("message", "")})
    feed = sorted((f for f in feed if f.get("t")), key=lambda f: f["t"], reverse=True)[:250]

    tick_t = _utc_epoch(loop.get("last_tick_utc"))
    return {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"), "now": now.timestamp(),
        "control": control_state(),
        "service": {"live": _unit_state(LIVE_SERVICE), "watchdog": _unit_state(WATCHDOG_SERVICE)},
        "loop": {"last_tick": tick_t, "decided_slot": loop.get("decided_slot"), "halt": loop.get("halt"),
                 "warned": loop.get("warned") or [], "flagged": loop.get("flagged") or [],
                 "flagged_at": loop.get("flagged_at") or {}, "reflag_after_min": LL.REFLAG_AFTER_MIN,
                 "recent_events": loop.get("recent_events") or [],
                 "in_window": LL.in_window(now), "next_decision_utc": LL.next_decision_utc(now),
                 "window_text": LL.window_text()},
        "snapshot": snap, "snapshot_t": feed_file.get("published_at") or _utc_epoch(feed_file.get("published_utc")),
        "claude": {"calls_today": len(today), "cost_today": round(sum(d["cost"] for d in today), 4),
                   "guard": policy.LIVE_DAILY_MAX_CALLS,
                   "account": Path(str((turns[-1] if turns else {}).get("account") or "")).name or None},
        "running": running, "queued": queued, "workers": by_kind,
        "feed": feed, "ledger": list(reversed(ledger_rows))[:80],
    }


def _is_live_id(run_id: str) -> bool:
    """Only calls on the LIVE queue are served from /live (runs/ also holds every training call)."""
    if not re.fullmatch(r"[0-9a-f]{6,40}", run_id or "") or not LIVE_DB.exists():
        return False
    con = sqlite3.connect(LIVE_DB, timeout=2)
    try:
        return con.execute("SELECT 1 FROM demands WHERE id = ?", (run_id,)).fetchone() is not None
    finally:
        con.close()


def live_run_detail(run_id: str) -> dict:
    """One live call in full: what it was asked, every step it took, what it concluded."""
    if not _is_live_id(run_id):
        return {"error": "no such live call"}
    hit = [d for d in _live_rows(100000) if d["id"] == run_id]
    if not hit:
        return {"error": "no such live call"}
    d = hit[0]
    p = _transcript_path(d)
    d["steps"] = transcript_steps(p, 400) if p else []
    d["transcript"] = str(p) if p else None
    return d


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>FTMO field — monitor</title>
<style>
:root { --fg:#1d1d1f; --bg:#fafafa; --mut:#6b6b6b; --line:#e3e3e3; --ok:#1a7f37; --bad:#b42318; --warn:#9a6700; --card:#fff; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --fg:#eaeaea; --bg:#121212; --mut:#9a9a9a; --line:#2c2c2c; --ok:#3fb950; --bad:#f85149; --warn:#d29922; --card:#1a1a1a; } }
body { font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; color:var(--fg); background:var(--bg); margin:0; padding:14px 18px; }
h1 { font-size:15px; margin:0 0 4px; } h2 { font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--mut); margin:18px 0 6px; }
.row { display:flex; gap:14px; flex-wrap:wrap; } .card { background:var(--card); border:1px solid var(--line); border-radius:6px; padding:10px 12px; min-width:200px; flex:1; }
.k { color:var(--mut); } .big { font-size:18px; } .ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)}
table { width:100%; border-collapse:collapse; } td, th { border-bottom:1px solid var(--line); padding:3px 6px; text-align:left; vertical-align:top; }
th { color:var(--mut); font-weight:normal; } .wrap { word-break:break-word; } .small { color:var(--mut); font-size:12px; }
.bar { height:6px; background:var(--line); border-radius:3px; overflow:hidden; } .bar > div { height:100%; background:var(--ok); }
button { font: 13px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; padding:6px 14px; border-radius:6px;
  border:1px solid var(--line); background:var(--card); color:var(--fg); cursor:pointer; }
button:hover { border-color:var(--mut); } button:disabled { opacity:.4; cursor:default; }
#controlcard.paused { border-color:var(--warn); } #controlcard.killed { border-color:var(--bad); }
.modebtn { font-weight:bold; } .modebtn.active.off { background:var(--line); color:var(--fg); }
.modebtn.active.training { background:var(--ok); color:#fff; border-color:var(--ok); }
.modebtn.active.live { background:var(--bad); color:#fff; border-color:var(--bad); }
</style></head><body>
<h1>FTMO field agent <span class="small" id="gen"></span>
 <a href="/board" class="small" style="margin-left:10px">workers &amp; board →</a>
 <a href="/live" class="small" style="margin-left:10px; color:var(--bad); font-weight:bold">LIVE →</a></h1>
<div class="card" id="modecard" style="margin-bottom:10px">
 <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
  <span class="k">mode:</span>
  <button class="modebtn off" id="btnModeOff" onclick="doMode('off')">OFF</button>
  <button class="modebtn training" id="btnModeTraining" onclick="doMode('training')">TRAINING</button>
  <button class="modebtn live" id="btnModeLive" onclick="doMode('live')">LIVE</button>
 </div>
 <div style="margin-top:6px" class="small" id="modeNote"></div>
</div>
<div class="card" id="controlcard" style="margin-bottom:10px">
 <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
  <button id="btnPause" onclick="doControl('pause')">⏸ Pause</button>
  <button id="btnResume" onclick="doControl('resume')">▶ Resume</button>
  <button id="btnKill" onclick="doKill()" style="color:var(--bad); border-color:var(--bad);">⏹ Kill</button>
  <span class="small" id="controlMsg"></span>
 </div>
 <div style="margin-top:8px"><span class="k">waiting for:</span> <b id="wait">…</b><br><span class="small" id="loop"></span></div>
</div>
<div class="card" id="bccard" style="margin-bottom:10px">
 <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
  <b>BLINDCHEAT2</b>
  <button onclick="bcControl('pause')">⏸ Pause</button>
  <button onclick="bcControl('resume')">▶ Resume</button>
  <button onclick="bcKill()" style="color:var(--bad); border-color:var(--bad);">⏹ Kill</button>
  <a href="/blindcheat" class="small">results →</a>
  <span class="small" id="bcMsg"></span>
 </div>
 <div style="margin-top:8px" class="small" id="bcLine">…</div>
</div>
<div class="row">
 <div class="card"><div class="k">equity</div><div class="big" id="eq">–</div><div class="small" id="bal"></div></div>
 <div class="card"><div class="k">window / epoch</div><div class="big" id="window">–</div><div class="bar"><div id="barfill" style="width:0"></div></div><div class="small" id="utc"></div></div>
 <div class="card"><div class="k">pass rate</div><div class="big" id="passrate">–</div><div class="small" id="passsub"></div></div>
 <div class="card"><div class="k">daily loss used</div><div class="big" id="dl">–</div><div class="bar"><div id="dlfill" style="width:0"></div></div><div class="small" id="dlsub"></div></div>
 <div class="card"><div class="k">total loss used</div><div class="big" id="tl">–</div><div class="bar"><div id="tlfill" style="width:0"></div></div><div class="small" id="tlsub"></div></div>
 <div class="card"><div class="k">spend</div><div class="big" id="cost">–</div><div class="small" id="costsub"></div></div>
 <div class="card"><div class="k">floating P&amp;L</div><div class="big" id="floating">–</div><div class="small" id="floatingsub"></div></div>
 <div class="card"><div class="k">trades (this attempt)</div><div class="big" id="tradecount">–</div><div class="small" id="tradecountsub"></div></div>
</div>
<h2>goal ladder</h2><div class="card"><table id="goals"></table></div>
<h2>now</h2><div class="card"><table id="queue"></table></div>
<h2>open positions (floating)</h2><div class="card"><table id="positions"></table></div>
<h2>trades by day — this 2-week window</h2><div class="card"><table id="bydayv"></table></div>
<h2>recent attempts (2-week runs)</h2><div class="card"><table id="attempts"></table></div>
<h2>recent decisions and results (newest first)</h2><div class="card"><table id="demands"></table></div>
<h2>fills and trade events</h2><div class="card"><table id="fills"></table></div>
<h2>turns</h2><div class="card"><table id="turns"></table></div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const t = ts => ts ? new Date(ts*1000).toLocaleTimeString() : '';
function pct(a,b){ return Math.min(100, Math.max(0, 100*a/b)); }
async function tick(){
  try {
    const d = await (await fetch('/api/status', {cache:'no-store'})).json();
    $('gen').textContent = 'updated ' + d.generated + (d.driver_running ? ' · loop running' : ' · loop NOT running');
    $('wait').textContent = d.waiting_for;
    $('loop').innerHTML = d.driver_running ? '<span class="ok">driver loop is running</span>' : '<span class="warn">no driver loop running</span>';
    const a = d.account;
    const e = a.equity - 10000;
    $('eq').innerHTML = a.equity.toFixed(2) + ' <span class="' + (e>=0?'ok':'bad') + '">' + (e>=0?'+':'') + e.toFixed(2) + '</span>';
    $('bal').textContent = 'balance ' + a.balance.toFixed(2) + ' · open risk ' + a.open_risk_usd + ' · traded days ' + a.traded_days + ' · rungs ' + a.rungs_passed + '/5';
    $('window').textContent = 'seg ' + a.window_index + '/' + a.windows_per_epoch + ' · attempt day ' + a.window_day + ' (no deadline) · epoch ' + a.epoch;
    $('barfill').style.width = Math.max(0, Math.min(100, (a.equity - 10000) / 10)) + '%';   // progress to +10%
    $('utc').textContent = a.utc + ' · attempt #' + a.attempt;
    $('passrate').textContent = (a.pass_rate==null ? '–' : (a.pass_rate*100).toFixed(1) + '%');
    $('passsub').textContent = a.attempts_passed + ' / ' + a.attempts_total + ' attempts passed';
    $('dl').textContent = Math.max(0, a.daily_loss_used_usd).toFixed(2) + ' / ' + a.daily_loss_limit_usd.toFixed(0);
    $('dlfill').style.width = pct(a.daily_loss_used_usd, a.daily_loss_limit_usd) + '%';
    $('dlsub').textContent = a.daily_loss_used_usd < 0 ? ('up ' + (-a.daily_loss_used_usd).toFixed(2) + ' today') : ('limit usd ' + a.daily_loss_limit_usd);
    $('tl').textContent = Math.max(0, a.total_loss_used_usd).toFixed(2) + ' / ' + a.total_loss_limit_usd.toFixed(0);
    $('tlfill').style.width = pct(a.total_loss_used_usd, a.total_loss_limit_usd) + '%';
    $('tlsub').textContent = 'breached: ' + (a.breached || 'no');
    $('cost').textContent = '$' + d.engine.cost_usd.toFixed(2) + (d.engine.cost_cap ? ' / $' + d.engine.cost_cap : ' at API prices (no cap: subscription)');
    $('costsub').textContent = d.engine.invocations + ' calls' + (d.engine.run_cap ? ' / ' + d.engine.run_cap : '') + ' · halt: ' + d.engine.halt;
    $('floating').innerHTML = '<span class="' + (a.floating_pnl_usd>=0?'ok':'bad') + '">' + (a.floating_pnl_usd>=0?'+':'') + a.floating_pnl_usd.toFixed(2) + '</span>';
    $('floatingsub').textContent = a.positions.length + ' open position' + (a.positions.length===1?'':'s') + ' carried into this turn';
    $('tradecount').textContent = a.trades_closed_this_attempt;
    $('tradecountsub').textContent = a.trades_closed_today + ' closed today (' + a.utc.slice(0,10) + ')';
    $('positions').innerHTML = a.positions.length ? '<tr><th>symbol</th><th>side</th><th>lots</th><th>entry</th><th>sl</th><th>tp</th><th>opened</th><th>floating</th></tr>' + a.positions.map(p =>
      `<tr><td>${esc(p.symbol)}</td><td>${esc(p.side)}</td><td>${p.lots}</td><td>${p.entry}</td><td>${p.sl ?? ''}</td><td>${p.tp ?? ''}</td><td>${esc(p.opened_utc)}</td><td class="${p.floating_usd>=0?'ok':'bad'}">${p.floating_usd>=0?'+':''}${p.floating_usd.toFixed(2)}</td></tr>`).join('')
      : '<tr><td class="small">no open positions — nothing carried between turns right now</td></tr>';
    const bd = Object.entries(a.trades_by_day_this_attempt || {});
    $('bydayv').innerHTML = bd.length ? '<tr><th>day</th><th>trades closed</th></tr>' + bd.map(([day,n]) =>
      `<tr><td>${esc(day)}</td><td>${n}</td></tr>`).join('') + `<tr><td class="small">attempt day ${a.window_day} (no deadline) · epoch ${a.epoch} · attempt #${a.attempt}</td><td></td></tr>`
      : '<tr><td class="small">no trades closed yet in this window</td></tr>';
    $('attempts').innerHTML = d.recent_attempts.length ? '<tr><th>attempt</th><th>epoch</th><th>window</th><th>outcome</th><th>final equity</th><th>traded days</th><th>dates</th></tr>' + d.recent_attempts.slice().reverse().map(x =>
      `<tr><td>#${x.attempt}</td><td>${x.epoch}</td><td>${x.window_index}</td><td class="${x.outcome==='passed'?'ok':(String(x.outcome).startsWith('breached')?'bad':'')}">${esc(x.outcome)}</td><td>${Number(x.final_equity).toFixed(2)}</td><td>${x.traded_days}</td><td class="small">${esc(x.start_day)} → ${esc(x.end_day)}</td></tr>`).join('')
      : '<tr><td class="small">no completed attempts yet</td></tr>';
    $('goals').innerHTML = '<tr><th>goal</th><th>passed</th><th>evidence</th></tr>' + d.goals.map(g =>
      `<tr><td>${esc(g.goal)}</td><td class="${g.passed?'ok':''}">${g.passed?'yes':'no'}</td><td class="small">${esc(g.evidence)}</td></tr>`).join('');
    const q = [...d.queue.active, ...d.queue.queued];
    $('queue').innerHTML = q.length ? '<tr><th>status</th><th>kind</th><th>source</th><th>waiting since</th></tr>' + q.map(x =>
      `<tr><td class="${x.status==='running'?'warn':''}">${x.status}</td><td>${esc(x.kind)}</td><td>${esc(x.source)}</td><td>${t(x.created)}</td></tr>`).join('')
      : '<tr><td class="small">nothing running or queued</td></tr>';
    $('demands').innerHTML = '<tr><th>time</th><th>kind</th><th>status</th><th>source</th><th>cost</th><th>denials</th><th>what it concluded</th></tr>' + d.recent_demands.map(x =>
      `<tr><td>${t(x.finished||x.created)}</td><td>${esc(x.kind)}</td><td class="${x.status==='done'?'ok':(x.status==='blocked'||x.status==='failed'?'bad':'')}">${esc(x.status)}</td><td class="small">${esc(x.source)}</td><td>$${x.cost.toFixed(3)}</td><td class="${x.denials?'bad':''}">${x.denials}</td><td class="wrap">${esc(x.summary)}</td></tr>`).join('');
    $('fills').innerHTML = d.fills.length ? '<tr><th>utc</th><th>event</th><th>detail</th></tr>' + d.fills.slice().reverse().map(r =>
      `<tr><td>${esc(r.utc)}</td><td>${esc(r.type)}</td><td class="wrap">${esc(JSON.stringify(Object.fromEntries(Object.entries(r).filter(([k])=>!['type','bar','utc'].includes(k)))).slice(0,260))}</td></tr>`).join('')
      : '<tr><td class="small">no fills yet: the agent has not placed an order</td></tr>';
    $('turns').innerHTML = '<tr><th>at</th><th>bar</th><th>equity</th><th>events</th><th>drain</th></tr>' + d.turns.slice().reverse().map(x =>
      `<tr><td>${esc(x.at)}</td><td>${x.bar}</td><td>${Number(x.equity).toFixed(2)}</td><td>${esc((x.events||[]).map(e=>e.event).join(', '))}</td><td>${esc(x.drain)}</td></tr>`).join('');
    const c = d.control, card = $('controlcard');
    ['btnModeOff','btnModeTraining','btnModeLive'].forEach(id => $(id).classList.remove('active'));
    $('btnMode' + c.mode[0] + c.mode.slice(1).toLowerCase()).classList.add('active');
    $('modeNote').textContent = c.mode_note || '';
    $('modeNote').style.color = c.live_alert ? 'var(--bad)' : '';
    $('modeNote').style.fontWeight = c.live_alert ? '700' : '';
    card.classList.remove('paused', 'killed');
    if (c.killed) {
      card.classList.add('killed');
      $('btnPause').disabled = true; $('btnResume').disabled = true; $('btnKill').disabled = true;
      $('controlMsg').innerHTML = '<span class="bad">KILLED — ask the operator to restart it. The dashboard cannot bring it back.</span>';
    } else if (c.paused_until) {
      card.classList.add('paused');
      $('btnPause').disabled = true; $('btnResume').disabled = false; $('btnKill').disabled = false;
      const when = new Date(c.paused_until.resume_at * 1000).toLocaleString();
      $('controlMsg').innerHTML = `<span class="warn">paused (${esc(c.paused_until.reason)}) — resumes automatically at ${when}, or click Resume now</span>`;
    } else if (c.manual_pause) {
      card.classList.add('paused');
      $('btnPause').disabled = true; $('btnResume').disabled = false; $('btnKill').disabled = false;
      $('controlMsg').innerHTML = '<span class="warn">paused manually — using zero resources except this dashboard. Click Resume to continue.</span>';
    } else {
      $('btnPause').disabled = false; $('btnResume').disabled = true; $('btnKill').disabled = false;
      if (!$('controlMsg').dataset.sticky) $('controlMsg').textContent = '';
    }
  } catch (err) { $('gen').textContent = 'cannot reach status: ' + err; }
}

async function bcTick(){
  try {
    const d = await (await fetch('/api/bc', {cache:'no-store'})).json();
    const e = d.phases.exam.paired_R_per_moment, st = d.status || {};
    $('bcLine').textContent = `${d.killed ? 'KILLED' : d.paused ? 'PAUSED' : d.active ? (st.state || 'running') : 'stopped'} | round ${st.round || '-'} ${st.phase || ''} | playbook v${d.playbook_version} | ${d.decisions} decisions, $${d.cost} | exam, playbook minus default per moment: ${e.n ? (e.mean >= 0 ? '+' : '') + e.mean + 'R (n=' + e.n + ')' : 'no exam yet'}${st.state === 'waiting' ? ' | ' + st.detail : ''}`;
  } catch (err) { $('bcLine').textContent = 'unreadable: ' + err; }
}
async function bcControl(action){
  $('bcMsg').textContent = 'working...';
  try {
    const r = await (await fetch('/api/bc/' + action, {method:'POST'})).json();
    $('bcMsg').textContent = r.ok ? '' : ('refused: ' + r.reason);
  } catch (err) { $('bcMsg').textContent = 'error: ' + err; }
  bcTick();
}
function bcKill(){
  if (!confirm('Kill stops BLINDCHEAT2 now and blocks Resume until the operator removes state/blindcheat2/KILLED. Its data stays. The assembly is not touched. Continue?')) return;
  bcControl('kill');
}
setInterval(bcTick, 10000); setTimeout(bcTick, 300);

async function doControl(action){
  $('controlMsg').dataset.sticky = '1'; $('controlMsg').textContent = 'working...';
  try {
    const r = await (await fetch('/api/' + action, {method:'POST'})).json();
    $('controlMsg').textContent = r.ok ? '' : ('refused: ' + r.reason);
  } catch (err) { $('controlMsg').textContent = 'error: ' + err; }
  delete $('controlMsg').dataset.sticky;
  tick();
}

function doKill(){
  if (!confirm('Kill stops the service now and disables it. You will need to ask the operator to restart it — this is not reversible from here. Continue?')) return;
  doControl('kill');
}

async function doMode(which){
  if (which === 'live' && !confirm('LIVE stops training and starts the live loop on whatever account is logged into the shared MT5 terminal. Real orders are sent only if state/LIVE_ARMED exists. Confirm you mean to select LIVE.')) return;
  $('modeNote').textContent = 'working...';
  try {
    const r = await (await fetch('/api/mode/' + which, {method:'POST'})).json();
    if (!r.ok) $('modeNote').textContent = 'refused: ' + r.reason;
  } catch (err) { $('modeNote').textContent = 'error: ' + err; }
  tick();
}
tick(); setInterval(tick, 3000);
</script></body></html>"""

BOARD_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>FTMO field — workers &amp; board</title>
<style>
:root { --fg:#1d1d1f; --bg:#fafafa; --mut:#6b6b6b; --line:#e3e3e3; --ok:#1a7f37; --bad:#b42318; --warn:#9a6700; --card:#fff; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --fg:#eaeaea; --bg:#121212; --mut:#9a9a9a; --line:#2c2c2c; --ok:#3fb950; --bad:#f85149; --warn:#d29922; --card:#1a1a1a; } }
body { font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; color:var(--fg); background:var(--bg); margin:0; padding:14px 18px; }
h1 { font-size:15px; margin:0 0 4px; } h2 { font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--mut); margin:18px 0 6px; }
.row { display:flex; gap:14px; flex-wrap:wrap; } .card { background:var(--card); border:1px solid var(--line); border-radius:6px; padding:10px 12px; min-width:200px; flex:1; }
.k { color:var(--mut); } .big { font-size:18px; } .ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)}
table { width:100%; border-collapse:collapse; } td, th { border-bottom:1px solid var(--line); padding:3px 6px; text-align:left; vertical-align:top; }
th { color:var(--mut); font-weight:normal; } .wrap { word-break:break-word; } .small { color:var(--mut); font-size:12px; }
a { color:inherit; }
</style></head><body>
<h1>Workers &amp; board <span class="small" id="gen"></span>
 <a href="/" class="small" style="margin-left:10px">← overview</a>
 <a href="/live" class="small" style="margin-left:10px; color:var(--bad); font-weight:bold">LIVE →</a></h1>
<div class="small" id="loop"></div>
<h2>workers (this build's specialist kinds, lifetime invocations)</h2>
<div class="card"><table id="workers"></table></div>
<h2>trades WITH vs WITHOUT prior board analysis (paper.py's own verified flag, not self-reported)</h2>
<div class="card">
 <table id="consulted_overall"></table>
 <div class="small" id="consulted_note" style="margin-top:6px"></div>
</div>
<div class="card" style="margin-top:10px"><table id="consulted_by_symbol"></table></div>
<h2>board posts (newest first — append-only: nothing here can be edited or deleted)</h2>
<div class="card"><table id="posts"></table></div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const t = ts => ts ? new Date(ts*1000).toLocaleTimeString() : '';
function pct(a){ return (a.n ? (a.win_rate*100).toFixed(0) + '%' : '–'); }
async function tick(){
  try {
    const d = await (await fetch('/api/board', {cache:'no-store'})).json();
    $('gen').textContent = 'updated ' + d.generated;
    $('loop').innerHTML = d.driver_running ? '<span class="ok">driver loop is running</span>' : '<span class="warn">no driver loop running</span>';

    const w = Object.entries(d.workers);
    $('workers').innerHTML = '<tr><th>worker</th><th>invocations</th><th>cost</th><th>may spawn</th><th>most recent</th></tr>' +
      w.map(([k, v]) => `<tr><td><b>${esc(k)}</b></td><td>${v.invocations}</td><td>$${v.cost_usd.toFixed(3)}</td>` +
        `<td class="small">${esc(v.may_spawn.join(', ') || '—')}</td>` +
        `<td class="wrap small">${v.recent.length ? esc(v.recent[0].summary).slice(0,160) : '—'}</td></tr>`).join('');

    const c = d.consulted, o = c.overall;
    $('consulted_overall').innerHTML = '<tr><th></th><th>trades</th><th>win rate</th><th>net USD</th></tr>' +
      `<tr><td>with prior analysis</td><td>${o.with_analysis.n}</td><td>${pct(o.with_analysis)}</td><td class="${o.with_analysis.net_usd>=0?'ok':'bad'}">${o.with_analysis.net_usd.toFixed(2)}</td></tr>` +
      `<tr><td>without</td><td>${o.without_analysis.n}</td><td>${pct(o.without_analysis)}</td><td class="${o.without_analysis.net_usd>=0?'ok':'bad'}">${o.without_analysis.net_usd.toFixed(2)}</td></tr>`;
    $('consulted_note').textContent = c.tracked_since;
    const bySym = Object.entries(c.by_symbol);
    $('consulted_by_symbol').innerHTML = bySym.length ? '<tr><th>symbol</th><th>with: n / win% / net</th><th>without: n / win% / net</th></tr>' +
      bySym.map(([s, v]) => `<tr><td>${esc(s)}</td>` +
        `<td>${v.with_analysis.n} / ${pct(v.with_analysis)} / ${v.with_analysis.net_usd.toFixed(2)}</td>` +
        `<td>${v.without_analysis.n} / ${pct(v.without_analysis)} / ${v.without_analysis.net_usd.toFixed(2)}</td></tr>`).join('')
      : '<tr><td class="small">no closed trades yet</td></tr>';

    $('posts').innerHTML = d.posts.length ? '<tr><th>utc</th><th>worker</th><th>topic</th><th>symbol</th><th>text</th></tr>' +
      d.posts.map(p => `<tr><td class="small">${esc(p.utc)}</td><td><b>${esc(p.worker)}</b></td><td>${esc(p.topic)}</td>` +
        `<td>${esc(p.symbol||'')}</td><td class="wrap">${esc(p.text)}</td></tr>`).join('')
      : '<tr><td class="small">the board is empty: no specialist has posted yet</td></tr>';
  } catch (err) { $('gen').textContent = 'cannot reach status: ' + err; }
}
tick(); setInterval(tick, 3000);
</script></body></html>"""

LIVE_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>FTMO field — live</title>
<style>
:root { --fg:#1d1d1f; --bg:#fafafa; --mut:#6b6b6b; --line:#e3e3e3; --ok:#1a7f37; --bad:#b42318; --warn:#9a6700; --acc:#0b62c4; --card:#fff; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --fg:#eaeaea; --bg:#121212; --mut:#9a9a9a; --line:#2c2c2c; --ok:#3fb950; --bad:#f85149; --warn:#d29922; --acc:#58a6ff; --card:#1a1a1a; } }
body { font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; color:var(--fg); background:var(--bg); margin:0; padding:14px 18px; }
h1 { font-size:15px; margin:0 0 8px; } h2 { font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--mut); margin:18px 0 6px; }
.row { display:flex; gap:14px; flex-wrap:wrap; } .card { background:var(--card); border:1px solid var(--line); border-radius:6px; padding:10px 12px; min-width:200px; flex:1; }
.k { color:var(--mut); } .big { font-size:18px; } .ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)} .acc{color:var(--acc)}
table { width:100%; border-collapse:collapse; } td, th { border-bottom:1px solid var(--line); padding:3px 6px; text-align:left; vertical-align:top; }
th { color:var(--mut); font-weight:normal; } .wrap { word-break:break-word; } .small { color:var(--mut); font-size:12px; } .nw { white-space:nowrap; }
.bar { height:6px; background:var(--line); border-radius:3px; overflow:hidden; margin-top:4px; } .bar > div { height:100%; background:var(--ok); }
a { color:inherit; }
#banner { padding:9px 12px; border-radius:6px; border:2px solid var(--line); font-weight:bold; margin-bottom:10px; }
#banner.ok { border-color:var(--ok); color:var(--ok); } #banner.bad { border-color:var(--bad); color:var(--bad); }
.tag { display:inline-block; min-width:52px; padding:0 5px; border-radius:4px; border:1px solid currentColor; font-size:11px; text-align:center; }
.tag.call{color:var(--acc)} .tag.board{color:var(--ok)} .tag.ledger{color:var(--warn)} .tag.loop{color:var(--mut)} .tag.error{color:var(--bad)}
tr.clickable { cursor:pointer; } tr.clickable:hover td { background:color-mix(in srgb, var(--line) 45%, transparent); }
.steps { margin:6px 0 4px; padding-left:10px; border-left:2px solid var(--line); }
.step { white-space:pre-wrap; word-break:break-word; margin:2px 0; } .step .when { color:var(--mut); margin-right:6px; }
.step.tool { color:var(--fg); } .step.result { color:var(--mut); } .step.say { color:var(--acc); } .step.answer { color:var(--ok); }
.step.err { color:var(--bad); }
.filters label { margin-right:14px; white-space:nowrap; } .filters { margin-bottom:6px; }
.pulse { animation: pulse 1.2s ease-in-out infinite; } @keyframes pulse { 50% { opacity:.35; } }
.wrap, .step { overflow-wrap:anywhere; }
@media (max-width: 640px) { body { padding:12px 16px; } .card { min-width:0; flex-basis:100%; }
  td, th { padding:3px 3px; } .hidephone { display:none; } .tag { min-width:0; }
  #feed tr, #ledger tr { display:grid; grid-template-columns:auto 1fr; column-gap:8px; border-bottom:1px solid var(--line); padding:3px 0; }
  #feed td, #ledger td { border-bottom:none; padding:1px 0; } #feed th, #ledger th { display:none; }
  #feed td:nth-child(3), #ledger td:nth-child(3) { grid-column:1 / -1; } .filters label { display:inline-block; margin:2px 10px 2px 0; } }
</style></head><body>
<h1>LIVE — the worker set on the real account <span class="small" id="gen"></span><br>
 <a href="/" class="small">← overview &amp; controls</a> <a href="/board" class="small" style="margin-left:10px">workers &amp; board (training) →</a></h1>
<div id="banner">…</div>
<div class="row">
 <div class="card"><div class="k">live loop</div><div class="big" id="loopbig">–</div><div class="small" id="loopsub"></div></div>
 <div class="card"><div class="k">account</div><div class="big" id="eq">–</div><div class="small" id="eqsub"></div></div>
 <div class="card"><div class="k">FTMO daily loss used</div><div class="big" id="dl">–</div><div class="bar"><div id="dlfill" style="width:0"></div></div><div class="small" id="dlsub"></div></div>
 <div class="card"><div class="k">FTMO total loss used</div><div class="big" id="tl">–</div><div class="bar"><div id="tlfill" style="width:0"></div></div><div class="small" id="tlsub"></div></div>
 <div class="card"><div class="k">risk</div><div class="big" id="risk">–</div><div class="small" id="risksub"></div></div>
 <div class="card"><div class="k">Claude calls today</div><div class="big" id="calls">–</div><div class="bar"><div id="callsfill" style="width:0"></div></div><div class="small" id="callssub"></div></div>
 <div class="card"><div class="k">own trades</div><div class="big" id="trades">–</div><div class="small" id="tradessub"></div></div>
</div>
<h2>now — what it is doing</h2><div class="card" id="now">…</div>
<h2>activity feed (newest first; click a call to see every step it took)</h2>
<div class="card">
 <div class="filters small" id="filters"></div>
 <table id="feed"></table>
</div>
<h2>positions</h2><div class="card"><table id="positions"></table><div class="small" id="others" style="margin-top:6px"></div></div>
<h2>live ledger (orders, refusals, closes, journal)</h2><div class="card"><table id="ledger"></table></div>
<h2>live workers (last 120 calls by kind)</h2><div class="card"><table id="workers"></table></div>
<h2>last move screen</h2><div class="card small" id="screen"></div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pad = n => String(n).padStart(2, '0');
const lt = ts => { if (!ts) return ''; const d = new Date(ts*1000); return pad(d.getHours())+':'+pad(d.getMinutes())+':'+pad(d.getSeconds()); };
const ut = ts => { if (!ts) return ''; const d = new Date(ts*1000); return pad(d.getUTCHours())+':'+pad(d.getUTCMinutes())+'z'; };
const when = ts => `<span class="nw">${lt(ts)}</span> <span class="small nw">${ut(ts)}</span>`;
const isoT = s => { const d = new Date(s); return isNaN(d) ? '' : pad(d.getHours())+':'+pad(d.getMinutes())+':'+pad(d.getSeconds()); };
const ago = s => s == null ? '' : (s < 90 ? Math.round(s) + 's' : (s < 5400 ? Math.round(s/60) + ' min' : (s/3600).toFixed(1) + ' h')) + ' ago';
const money = (v, cur) => v == null ? '–' : Number(v).toFixed(2) + (cur ? ' ' + cur : '');
function pct(a,b){ return b ? Math.min(100, Math.max(0, 100*a/b)) : 0; }
const SRC = ['call','board','ledger','loop','error'];
const LABEL = {call:'Claude calls', board:'board posts', ledger:'ledger', loop:'loop turns', error:'errors'};
let show = {call:true, board:true, ledger:true, loop:true, error:true};
try { Object.assign(show, JSON.parse(localStorage.getItem('liveFilters') || '{}')); } catch (e) {}
const openIds = new Set(), details = {};
let last = null;

$('filters').innerHTML = SRC.map(s => `<label><input type="checkbox" data-src="${s}" ${show[s]?'checked':''}> <span class="tag ${s}">${s}</span> ${LABEL[s]}</label>`).join('');
$('filters').addEventListener('change', e => {
  const s = e.target.dataset.src; if (!s) return;
  show[s] = e.target.checked;
  try { localStorage.setItem('liveFilters', JSON.stringify(show)); } catch (err) {}
  render();
});

function stepsHtml(steps){
  if (!steps || !steps.length) return '<div class="small">no steps recorded (yet)</div>';
  const icon = {tool:'▶', result:'←', say:'»', answer:'✔'};
  return '<div class="steps">' + steps.map(s =>
    `<div class="step ${s.kind}${s.error?' err':''}"><span class="when">${isoT(s.t)}</span>${icon[s.kind]||''} ${s.tool && s.tool!=='Bash' ? '['+esc(s.tool)+'] ' : ''}${esc(s.text)}</div>`).join('') + '</div>';
}

function detailHtml(id){
  const d = details[id];
  if (!d) return '<div class="small pulse">loading the steps…</div>';
  if (d.error && !d.id) return `<div class="bad">${esc(d.error)}</div>`;
  let h = '';
  if (d.findings && d.findings.length) h += '<div class="k" style="margin-top:4px">findings</div><ul style="margin:2px 0 4px 18px;padding:0">' + d.findings.map(f => `<li>${esc(f)}</li>`).join('') + '</ul>';
  if (d.next_demands && d.next_demands.length) h += `<div><span class="k">handed off to:</span> ${esc(d.next_demands.join(', '))}</div>`;
  if (d.denials) h += `<div class="bad">${d.denials} permission denial(s)</div>`;
  h += `<div class="k" style="margin-top:4px">every step (${d.steps.length})</div>` + stepsHtml(d.steps);
  h += `<div class="small"><a href="/live/run/${esc(d.id)}.md" target="_blank">full prompt + answer →</a>${d.transcript ? ' · transcript ' + esc(d.transcript) : ''}</div>`;
  return h;
}

async function loadDetail(id){
  try { details[id] = await (await fetch('/api/live/run/' + id, {cache:'no-store'})).json(); }
  catch (e) { details[id] = {error: String(e)}; }
  render();
}
function toggle(id){
  if (openIds.has(id)) openIds.delete(id); else { openIds.add(id); loadDetail(id); }
  render();
}

function render(){
  const d = last; if (!d) return;
  const c = d.control, s = d.snapshot || {}, L = d.loop, now = d.now;
  $('gen').textContent = 'updated ' + d.generated + ' · times: your clock, then UTC (z)';
  const trading = c.mode === 'LIVE' && c.live_armed && c.broker_connected && d.service.live === 'active';
  const why = [];
  if (c.mode !== 'LIVE') why.push('mode is ' + c.mode);
  if (!c.live_armed) why.push('NOT ARMED (state/LIVE_ARMED missing)');
  if (!c.broker_connected) why.push('broker not connected');
  if (d.service.live !== 'active') why.push('ftmo-live.service is ' + d.service.live);
  if (s.own_halt) why.push('own halt: ' + s.own_halt);
  if (s.entry_block) why.push('entry block: ' + s.entry_block);
  if (L.halt) why.push('loop halt: ' + JSON.stringify(L.halt));
  $('banner').className = trading && !s.own_halt && !s.entry_block ? 'ok' : 'bad';
  $('banner').textContent = (trading ? 'REALLY TRADING' : 'NOT TRADING') +
    (s.login ? ' · account ' + s.login + ' ' + (s.currency||'') : '') +
    (why.length ? ' · ' + why.join(' · ') : ' · armed · broker connected · loop running') +
    ' · watchdog ' + d.service.watchdog;

  const tickAge = L.last_tick ? now - L.last_tick : null;
  const stale = L.in_window && tickAge != null && tickAge > 420;
  $('loopbig').innerHTML = `<span class="${d.service.live==='active'?(stale?'bad':'ok'):'bad'}">${esc(d.service.live)}</span>`;
  $('loopsub').innerHTML = `last tick ${lt(L.last_tick)} (<span class="${stale?'bad':''}">${ago(tickAge)}</span>)<br>` +
    (L.in_window ? '<span class="ok">window open</span>' : '<span class="warn">outside the window — no new decisions</span>') +
    (L.next_decision_utc ? ' · next decision ' + esc(L.next_decision_utc) : '') +
    `<br>move screen every 5-min bar · slot ${esc(L.decided_slot||'–')}`;

  const pnl = s.day_pnl;
  $('eq').innerHTML = s.equity != null ? money(s.equity, s.currency) : '–';
  $('eqsub').innerHTML = s.equity != null ? `balance ${money(s.balance)} · day <span class="${pnl>=0?'ok':'bad'}">${pnl>=0?'+':''}${money(pnl)}</span><br>snapshot ${ut(d.snapshot_t)} (${ago(d.snapshot_t ? now - d.snapshot_t : null)}) · market ${s.market_open?'open':'closed'}` : 'no live snapshot yet (the loop publishes one every 5-min tick)';
  $('dl').textContent = s.daily_loss_limit != null ? Math.max(0, s.daily_loss_used).toFixed(2) + ' / ' + s.daily_loss_limit.toFixed(0) : '–';
  $('dlfill').style.width = pct(s.daily_loss_used, s.daily_loss_limit) + '%';
  $('dlsub').innerHTML = s.daily_loss_limit != null ? `own day P&amp;L ${money(s.own_day_pnl)} · day start ${money(s.day_start_equity)}` : '';
  $('tl').textContent = s.total_loss_limit != null ? Math.max(0, s.total_loss_used).toFixed(2) + ' / ' + s.total_loss_limit.toFixed(0) : '–';
  $('tlfill').style.width = pct(s.total_loss_used, s.total_loss_limit) + '%';
  $('tlsub').textContent = s.initial_balance != null ? 'initial balance ' + money(s.initial_balance, s.currency) : '';
  $('risk').textContent = s.risk_pct != null ? (s.risk_pct*100).toFixed(2) + '% / trade' : '–';
  $('risksub').innerHTML = s.risk_pct != null ? `cap ${money(s.per_trade_cap, s.currency)} · own open risk ${money(s.own_open_risk)}<br>own positions ${(s.own_positions||[]).length}` : '';
  const cl = d.claude;
  $('calls').textContent = cl.calls_today + ' / ' + cl.guard;
  $('callsfill').style.width = pct(cl.calls_today, cl.guard) + '%';
  $('callssub').textContent = '$' + cl.cost_today.toFixed(2) + ' at API prices · account ' + (cl.account||'?') + ' · ' + d.queued.length + ' queued';
  const st = s.own_closed_trades || {};
  $('trades').textContent = (s.own_trades_today ?? '–') + ' today';
  $('tradessub').innerHTML = st.closed != null ? `closed ${st.closed} (W ${st.wins} / L ${st.losses}) · net ${money(st.net)}${st.mean_r_net!=null?' · mean '+st.mean_r_net+'R':''}<br>traded days ${(s.own_traded_days||[]).length}` : '';

  let nowH = '';
  if (d.running.length) {
    nowH = d.running.map(r => `<div><b class="acc pulse">● RUNNING</b> <b>${esc(r.kind)}</b> <span class="small">${esc(r.source)} · started ${lt(r.started)} · ${r.elapsed}s so far</span></div>` + stepsHtml(r.steps)).join('');
  } else {
    nowH = `<div><b>idle</b> <span class="small">— waiting for the next 5-min screen` + (L.next_decision_utc ? ' or the ' + esc(L.next_decision_utc) + ' decision turn' : '') + '</span></div>';
  }
  if (d.queued.length) nowH += '<div class="warn" style="margin-top:6px">queued: ' + d.queued.map(q => esc(q.kind) + ' (' + esc(q.source) + ')').join(', ') + '</div>';
  $('now').innerHTML = nowH;

  const rows = d.feed.filter(f => show[f.src]);
  $('feed').innerHTML = rows.length ? '<tr><th>time</th><th>what</th><th>detail</th></tr>' + rows.map(f => {
    if (f.src === 'call') {
      const col = f.status === 'done' ? 'ok' : (f.status === 'running' ? 'acc pulse' : 'bad');
      const meta = `<span class="${col}">${esc(f.status)}</span>` + (f.secs!=null ? ` · ${f.secs}s` : '') + (f.cost ? ` · $${f.cost.toFixed(3)}` : '') +
        (f.next && f.next.length ? ' · → ' + esc(f.next.join(', ')) : '') + (f.denials ? ` · <span class="bad">${f.denials} denied</span>` : '');
      const open = openIds.has(f.id);
      return `<tr class="clickable" onclick="toggle('${esc(f.id)}')"><td>${when(f.t)}</td><td class="nw"><span class="tag call">call</span> <b>${esc(f.kind)}</b></td>` +
        `<td class="wrap">${open?'▾':'▸'} <span class="small">${meta}</span><br>${esc(f.text)}${open ? detailHtml(f.id) : ''}</td></tr>`;
    }
    if (f.src === 'board') return `<tr><td>${when(f.t)}</td><td class="nw"><span class="tag board">board</span> ${esc(f.worker)}</td><td class="wrap"><span class="small">${esc(f.topic)}${f.symbol?' · '+esc(f.symbol):''}</span><br>${esc(f.text)}</td></tr>`;
    if (f.src === 'ledger') {
      const col = f.type === 'order_placed' ? 'ok' : (/refused|error|failed/.test(f.type) ? 'bad' : '');
      return `<tr><td>${when(f.t)}</td><td class="nw"><span class="tag ledger">ledger</span> <span class="${col}">${esc(f.type)}</span></td><td class="wrap">${esc(f.text)}</td></tr>`;
    }
    if (f.src === 'error') return `<tr><td>${when(f.t)}</td><td class="nw"><span class="tag error">error</span></td><td class="wrap bad">${esc(f.text)}</td></tr>`;
    return `<tr><td>${when(f.t)}</td><td class="nw"><span class="tag loop">loop</span></td><td class="wrap small">${esc(f.text)}</td></tr>`;
  }).join('') : '<tr><td class="small">nothing yet with these filters</td></tr>';

  const own = s.own_positions || [];
  $('positions').innerHTML = own.length ? '<tr><th>ticket</th><th>symbol</th><th>side</th><th>lots</th><th>entry</th><th>sl</th><th>tp</th><th>opened</th><th>floating</th></tr>' + own.map(p =>
    `<tr><td>${p.ticket}</td><td>${esc(p.symbol)}</td><td>${esc(p.side)}</td><td>${p.lots}</td><td>${p.entry}</td><td>${p.sl ?? ''}</td><td>${p.tp ?? ''}</td><td>${esc(p.opened_utc)}</td><td class="${p.floating>=0?'ok':'bad'}">${p.floating>=0?'+':''}${money(p.floating)}</td></tr>`).join('')
    : '<tr><td class="small">the assembly holds no position right now</td></tr>';
  const oth = Object.entries(s.other_runners_positions || {});
  $('others').textContent = 'other runners on this account (it may not take the opposite side of these): ' + (oth.length ? oth.map(([k,n]) => k + (n>1?' ×'+n:'')).join(', ') : 'none');

  $('ledger').innerHTML = d.ledger.length ? '<tr><th>time</th><th>event</th><th>detail</th></tr>' + d.ledger.map(r => {
    const col = r.type === 'order_placed' ? 'ok' : (/refused|error|failed/.test(r.type) ? 'bad' : '');
    return `<tr><td>${r.t ? when(r.t) : '<span class="small">no time</span>'}</td><td class="nw ${col}">${esc(r.type)}</td><td class="wrap">${esc(r.text)}</td></tr>`;
  }).join('') : '<tr><td class="small">nothing in the live ledger yet</td></tr>';

  $('workers').innerHTML = '<tr><th>worker</th><th>today</th><th class="hidephone">recent</th><th class="hidephone">cost</th><th>last</th><th>last said</th></tr>' + Object.entries(d.workers).map(([k, w]) =>
    `<tr><td class="wrap"><b>${esc(k)}</b></td><td>${w.today}</td><td class="hidephone">${w.recent}</td><td class="hidephone">$${w.cost.toFixed(3)}</td><td class="nw">${w.last_t ? lt(w.last_t) : '—'}</td><td class="wrap small">${esc(w.last) || '—'}</td></tr>`).join('');

  const ev = (L.recent_events || []).map(e => esc(e.type) + ': ' + (e.candidates ? e.candidates.map(x => `${esc(x.symbol)} ${x.pct_move>0?'+':''}${x.pct_move}%`).join(', ') : esc(JSON.stringify(e).slice(0, 200)))).join('<br>');
  const fa = L.flagged_at || {}, gap = L.reflag_after_min || 0;
  const again = s => { const t = fa[s] ? Date.parse(fa[s].replace(' ', 'T') + 'Z') / 1000 : null;
    return t && gap ? ` (again from ${lt(t + gap*60)})` : ''; };
  $('screen').innerHTML = (gap ? `waiting out their ${gap} min before they can trigger the scout again: ` : 'flagged today: ') +
    `${(L.flagged||[]).map(s => esc(s) + `<span class="small">${again(s)}</span>`).join(', ') || 'none'}` +
    (ev ? '<br>' + ev : '') + `<br>${esc(L.window_text)}`;
}

async function tick(){
  try {
    last = await (await fetch('/api/live', {cache:'no-store'})).json();
    for (const id of openIds) {   // a running call grows every tick; a finished one is fetched once more for its final steps
      const f = last.feed.find(x => x.id === id);
      if (f && (f.status === 'running' || (details[id] && details[id].status && details[id].status !== f.status))) loadDetail(id);
    }
    render();
  } catch (err) { $('gen').textContent = 'cannot reach status: ' + err; }
}
tick(); setInterval(tick, 3000);
</script></body></html>"""


MODES = ("OFF", "TRAINING", "LIVE")
MODE_FILE = HOME / "state" / "MODE"


def read_mode() -> str:
    """TRAINING is the default (today's only real behaviour) whenever no one has ever switched
    modes -- an absent file must never silently mean something other than the system's whole
    history so far."""
    if MODE_FILE.exists():
        m = MODE_FILE.read_text().strip()
        if m in MODES:
            return m
    return "TRAINING"


_broker_conn_cache = {"ts": 0.0, "val": False}


def _broker_connected_cached(ttl: float = 5.0) -> bool:
    """connected() now dials the real shared MT5 bridge (tools/live_broker.py) -- fine for an
    occasional order, too chatty for a dashboard the operator's browser polls every 3s. A short
    TTL cache here (not inside live_broker.py itself, which stays always-fresh for the
    order/close/modify path where staleness would actually matter) keeps that polling cheap."""
    now = time.time()
    if now - _broker_conn_cache["ts"] > ttl:
        _broker_conn_cache["val"] = LB.connected()
        _broker_conn_cache["ts"] = now
    return _broker_conn_cache["val"]


def _training_usage() -> str:
    """tools/castigation.py's meter, read-only (2026-11-01: 25% of a usage window without an order castigates)."""
    try:
        import castigation
        return castigation.peek(HOME)
    except Exception as exc:
        return f"usage meter unreadable: {exc}"


def control_state() -> dict:
    """What the control buttons should show. KILLED and MANUAL_PAUSE are separate markers: a kill is
    one-way from this UI (the operator restarts it by hand); a manual pause has its own Resume button."""
    killed = (HOME / "state" / "KILLED").exists()
    manual_pause = (HOME / "state" / "MANUAL_PAUSE").exists() or (
        read_mode() == "TRAINING" and (HOME / "state" / "TRAINING_CASTIGATION_STOP").exists())   # shows Resume
    marker = HOME / "state" / "paused_until.json"
    paused_until = json.loads(marker.read_text()) if marker.exists() else None
    mode = read_mode()
    live_armed = (HOME / "state" / "LIVE_ARMED").exists()
    broker_connected = _broker_connected_cached()
    mode_note = {
        "OFF": "nothing runs: no replay, no live loop, no Claude calls (the live watchdog still guards open positions).",
        "TRAINING": "the historical replay (backtest) assembly; " + _training_usage(),
        "LIVE": ("broker connected and worker armed -- really trading. " if (live_armed and broker_connected)
                 else "LIVE selected, but " + (
                     "no broker is connected" if not broker_connected
                     else "the live-fill worker is not armed") + " -- nothing will actually trade. ") + live_brief(),
    }[mode]
    return {"killed": killed, "manual_pause": manual_pause, "paused_until": paused_until,
            "mode": mode, "mode_note": mode_note, "live_armed": live_armed, "broker_connected": broker_connected,
            "live_alert": "⚠" in mode_note}


TRAINING_SERVICE = "ftmo-field.service"
LIVE_SERVICE = "ftmo-live.service"
WATCHDOG_SERVICE = "ftmo-live-watchdog.service"
MODE_SERVICE = {"TRAINING": TRAINING_SERVICE, "LIVE": LIVE_SERVICE}


def _systemctl(verb: str, unit: str) -> None:
    subprocess.run(["systemctl", "--user", verb, unit])


def live_brief() -> str:
    """One line for the mode card in LIVE: the loop's last tick, today's live spend, the live queue.
    Plain reads (a JSON file and one sqlite query), cheap enough for the 3s poll."""
    import sqlite3
    parts = []
    alert = HOME / "state" / "live_alert.json"
    try:
        a = json.loads(alert.read_text())
        from datetime import datetime
        from zoneinfo import ZoneInfo
        if a.get("day") == datetime.now(ZoneInfo("Europe/Prague")).date().isoformat():
            parts.append("⚠ " + a.get("message", "RUNAWAY GUARD TRIPPED"))
    except (OSError, json.JSONDecodeError):
        pass
    loop = HOME / "state" / "live_loop.json"
    try:
        parts.append(f"live loop last tick {json.loads(loop.read_text()).get('last_tick_utc', '?')} UTC")
    except (OSError, json.JSONDecodeError):
        parts.append("live loop has not ticked yet")
    db = HOME / "state" / "live_selfcall.db"
    if db.exists():
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            start = datetime.now(ZoneInfo("Europe/Prague")).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            con = sqlite3.connect(db)
            spent, calls = con.execute("SELECT COALESCE(SUM(cost_usd),0), COUNT(*) FROM demands WHERE started >= ?",
                                       (start,)).fetchone()
            queued = con.execute("SELECT COUNT(*) FROM demands WHERE status='queued'").fetchone()[0]
            con.close()
            parts.append(f"today {calls} calls (${spent:.2f} at API prices; runaway guard "
                         f"{policy.LIVE_DAILY_MAX_CALLS}), {queued} queued")
        except Exception as exc:
            parts.append(f"live queue unreadable: {exc}")
    return "; ".join(parts) + "."


def do_pause() -> dict:
    (HOME / "state" / "MANUAL_PAUSE").write_text("paused from the dashboard\n")
    (HOME / "state" / "STOP").write_text("paused from the dashboard\n")
    _systemctl("stop", TRAINING_SERVICE)
    _systemctl("stop", LIVE_SERVICE)
    return {"ok": True, "state": "paused"}


def do_resume() -> dict:
    if (HOME / "state" / "KILLED").exists():
        return {"ok": False, "reason": "killed: not resumable from here, ask the operator to restart it"}
    for name in ("MANUAL_PAUSE", "STOP", "TRAINING_CASTIGATION_STOP"):   # the last: training's own castigation stop
        p = HOME / "state" / name
        if p.exists():
            p.unlink()
    marker = HOME / "state" / "paused_until.json"
    if marker.exists():
        marker.unlink()
    unit = MODE_SERVICE.get(read_mode())  # resume whatever the master switch selects; OFF resumes nothing
    if unit:
        _systemctl("start", unit)
    return {"ok": True, "state": "resumed" if unit else "resumed (mode OFF: nothing to start)"}


def do_kill() -> dict:
    (HOME / "state" / "KILLED").write_text("killed from the dashboard; ask the operator to bring it back\n")
    (HOME / "state" / "STOP").write_text("killed\n")
    for unit in (TRAINING_SERVICE, LIVE_SERVICE):
        _systemctl("stop", unit)
        _systemctl("disable", unit)
    return {"ok": True, "state": "killed"}


def do_set_mode(mode: str) -> dict:
    """Each mode has its own service: TRAINING runs ftmo-field (the replay), LIVE runs ftmo-live
    (driver.py live-run), OFF runs neither. Switching stops the other one FIRST, so the replay and
    the live loop never run at the same time (they would share one Claude usage window -- todos/002).
    The watchdog (Friday flatten, rollover guard) is started on LIVE and never stopped by a mode
    switch: a real position must stay protected after the switch moves away from LIVE.
    This is a separate marker from MANUAL_PAUSE/KILLED on purpose: the master switch answers "which
    world", pause/resume/kill answer "is it running" -- a mode switch does not touch either marker
    (a manual pause keeps the new mode's service idle), and a kill still overrides everything."""
    if (HOME / "state" / "KILLED").exists():
        return {"ok": False, "reason": "killed: not controllable from here, ask the operator to restart it"}
    MODE_FILE.write_text(mode)
    for other, unit in MODE_SERVICE.items():
        if other != mode:
            _systemctl("stop", unit)
    if mode in MODE_SERVICE:
        if (HOME / "state" / "STOP").exists() and not (HOME / "state" / "MANUAL_PAUSE").exists():
            (HOME / "state" / "STOP").unlink()
        if mode == "LIVE":
            _systemctl("start", WATCHDOG_SERVICE)
        _systemctl("start", MODE_SERVICE[mode])
    return {"ok": True, "mode": mode}


def do_mode_off() -> dict:
    return do_set_mode("OFF")


def do_mode_training() -> dict:
    return do_set_mode("TRAINING")


def do_mode_live() -> dict:
    return do_set_mode("LIVE")


# ---- BLINDCHEAT2 (tools/blindcheat2.py, todos/011): its own service and its own markers. The assembly's Pause, Resume and Kill never touch it,
# and these never touch the assembly.
BLINDCHEAT_SERVICE = "ftmo-blindcheat.service"


def _bc():
    import blindcheat2
    return blindcheat2


def bc_active() -> bool:
    return subprocess.run(["systemctl", "--user", "is-active", "-q", BLINDCHEAT_SERVICE]).returncode == 0


def do_bc_pause() -> dict:
    B = _bc()
    B.ST.mkdir(parents=True, exist_ok=True)
    B.PAUSED.write_text("paused from the dashboard\n")
    _systemctl("stop", BLINDCHEAT_SERVICE)
    return {"ok": True, "state": "paused"}


def do_bc_resume() -> dict:
    B = _bc()
    if B.KILLED.exists():
        return {"ok": False, "reason": "killed: remove state/blindcheat2/KILLED by hand to allow Resume"}
    if B.PAUSED.exists():
        B.PAUSED.unlink()
    _systemctl("start", BLINDCHEAT_SERVICE)
    return {"ok": True, "state": "resumed"}


def do_bc_kill() -> dict:
    B = _bc()
    B.ST.mkdir(parents=True, exist_ok=True)
    B.KILLED.write_text("killed from the dashboard\n")
    _systemctl("stop", BLINDCHEAT_SERVICE)
    return {"ok": True, "state": "killed"}


def bc_status() -> dict:
    return {**_bc().summary(), "active": bc_active()}


BC_PAGE = PAGE[:PAGE.index("</style>") + 8] + """</head><body>
<h1>BLINDCHEAT2 <a href="/" class="small" style="margin-left:10px">← overview &amp; controls</a></h1>
<div class="card" style="margin-bottom:10px"><div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
 <button onclick="act('pause')">⏸ Pause</button><button onclick="act('resume')">▶ Resume</button>
 <button onclick="kill()" style="color:var(--bad); border-color:var(--bad);">⏹ Kill</button><span class="small" id="msg"></span></div>
 <div class="small" id="line" style="margin-top:8px">…</div></div>
<div class="card" style="margin-bottom:10px"><b>Does the playbook help?</b> <span class="small">Exam moments only: later dates the model cannot recall, results never fed back. Same moment, playbook vs the default model.</span>
 <div id="verdict" style="margin:6px 0"></div><div id="phases"></div></div>
<div class="card" style="margin-bottom:10px"><b>R per trade, in order</b> <span class="small">cumulative, blind and exam together</span><div id="curve"></div></div>
<div class="card" style="margin-bottom:10px"><b>Stops, manager, costs, speed</b> <div id="stops" class="small"></div><div id="eff" class="small" style="margin-top:4px"></div></div>
<div class="card" id="tradecard" style="margin-bottom:10px;display:none"><b>Saved trade</b> <span class="small" id="tradeid"></span><div id="tradechart"></div><div id="tradeev" class="small"></div></div>
<details class="card" style="margin-bottom:10px" open><summary><b>Current playbook</b> <span class="small" id="pbv"></span></summary><pre id="book" style="white-space:pre-wrap"></pre></details>
<details class="card" style="margin-bottom:10px"><summary><b>By playbook version</b></summary><div id="versions"></div></details>
<details class="card" style="margin-bottom:10px"><summary><b>By setup</b></summary><div id="setups"></div></details>
<details class="card" style="margin-bottom:10px"><summary><b>Latest lessons from studied charts</b> <span class="small" id="studyn"></span></summary><div id="lessons" class="small"></div></details>
<details class="card" style="margin-bottom:10px" open><summary><b>Latest decisions</b></summary><div id="recent" style="overflow-x:auto"></div></details>
<details class="card"><summary><b>Errors</b></summary><div id="errors" class="small"></div></details>
<script>
const $ = id => document.getElementById(id);
const esc = t => String(t ?? '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const sgn = x => x == null ? '-' : (x >= 0 ? '+' : '') + x;
const tbl = (head, rows) => '<table><tr>' + head.map(h => '<th>' + esc(h) + '</th>').join('') + '</tr>' + rows.map(r => '<tr>' + r.map(c => '<td>' + c + '</td>').join('') + '</tr>').join('') + '</table>';
function curve(c){
  const W = 900, H = 170, cum = a => { let s = 0; return [0, ...a.map(x => s += x)]; }, p = cum(c.playbook), d = cum(c.default);
  const n = Math.max(p.length, d.length, 2), all = [...p, ...d, 0], lo = Math.min(...all), hi = Math.max(...all, lo + 1);
  const pts = a => a.map((y, i) => `${(i / (n - 1) * W).toFixed(1)},${(H - 8 - (y - lo) / (hi - lo) * (H - 16)).toFixed(1)}`).join(' ');
  const z = (H - 8 - (0 - lo) / (hi - lo) * (H - 16)).toFixed(1);
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto"><line x1="0" x2="${W}" y1="${z}" y2="${z}" stroke="var(--line)"/><polyline fill="none" stroke="var(--ok)" stroke-width="2" points="${pts(p)}"/><polyline fill="none" stroke="var(--mut)" stroke-width="2" points="${pts(d)}"/></svg><div class="small"><span class="ok">with playbook ${sgn(p[p.length-1].toFixed(1))}R</span> · default ${sgn(d[d.length-1].toFixed(1))}R</div>`;
}
async function tick(){
  const d = await (await fetch('/api/bc', {cache:'no-store'})).json(), st = d.status || {};
  $('line').textContent = `${d.killed ? 'KILLED' : d.paused ? 'PAUSED' : d.active ? (st.state || 'running') : 'stopped'} | round ${st.round || '-'} ${st.phase || ''} | ${d.decisions} decisions, $${d.cost} | ${st.detail || ''}`;
  const e = d.phases.exam.paired_R_per_moment;
  $('verdict').innerHTML = !e.n ? '<span class="warn">No exam yet: the first comes at round 4.</span>'
    : e.n < 100 ? `<span class="warn">Too early: ${e.n} exam moments, ${sgn(e.mean)}R per moment (se ${e.se}). Wait for about 100.</span>`
    : Math.abs(e.mean) < 2 * e.se ? `No difference shown: ${sgn(e.mean)}R per moment, se ${e.se}, n=${e.n}.`
    : `<b class="${e.mean > 0 ? 'ok' : 'bad'}">${e.mean > 0 ? 'The playbook helps' : 'The playbook hurts'}: ${sgn(e.mean)}R per moment, se ${e.se}, n=${e.n}.</b>`;
  const row = (nm, a) => [nm, a.moments, a.trades, a.take == null ? '-' : Math.round(a.take * 100) + '%', sgn(a.mean), a.se ?? '-', a.win == null ? '-' : Math.round(a.win * 100) + '%', sgn(a.hands_off), sgn(a.manager_value), sgn(a.side_skill), '$' + a.swap_usd + ' / $' + a.commission_usd, esc(Object.entries(a.outcomes).map(([k, v]) => k + ' ' + v).join(', '))];
  $('phases').innerHTML = tbl(['', 'moments', 'trades', 'take', 'R/trade net', 'se', 'win', 'hands-off', 'manager adds', 'vs other side', 'swap / comm', 'outcomes'],
    [row('exam, playbook', d.phases.exam.playbook), row('exam, default', d.phases.exam.default), row('learn, playbook', d.phases.learn.playbook), row('learn, default', d.phases.learn.default)]);
  $('curve').innerHTML = curve(d.curve);
  const s = d.stop;
  $('stops').textContent = s.stopped ? `${s.stopped} trades stopped; ${s.then_target} of them (${Math.round(s.share * 100)}%) then reached their target. Those went a median ${s.median_R_against}R against first.`
    + (s.mirror_stopped ? ` The opposite trade at the same moments (same distances): ${s.mirror_then_target} of ${s.mirror_stopped} stopped (${Math.round(s.mirror_share * 100)}%) then reached its target. Similar shares mean the stop is not the problem; the 5-day window is.` : '') : 'No stops yet.';
  const sp = d.speed || {}; $('eff').textContent = `${sp.decisions_per_hour ?? '-'} decisions/hour, $${sp.cost_per_decision ?? '-'} per decision, $${sp.cost_per_trade ?? '-'} per trade, ${sp.trades ?? 0} trades.`;
  $('pbv').textContent = 'v' + d.playbook_version; $('book').textContent = d.playbook || '(none yet)';
  $('versions').innerHTML = tbl(['v', 'trades', 'R/trade', 'win', 'what changed'], d.versions.map(v => [v.version, v.n, sgn(v.mean), Math.round((v.win || 0) * 100) + '%', esc(v.changed)]));
  $('setups').innerHTML = tbl(['setup', 'trades', 'R/trade', 'win', 'sum'], d.setups.map(v => [esc(v.setup), v.n, sgn(v.mean), Math.round((v.win || 0) * 100) + '%', sgn(v.sum)]));
  const y = d.study; $('studyn').textContent = `${y.charts} charts, ${y.checked} marked trades checked on the bars, ${y.wins_on_bars} reached target first`;
  $('lessons').innerHTML = d.recent_lessons.map(l => `<p><b>If</b> ${esc(l.if)} <b>then</b> ${esc(l.then)} <b>fails when</b> ${esc(l.fails_when)}</p>`).join('') || '(none)';
  $('recent').innerHTML = tbl(['phase', 'v', 'arm', 'utc', 'symbol', 'action', 'setup', 'outcome', 'exit', 'R net', 'hands-off', 'rr', 'wakes', 'reason'],
    d.recent.map(r => [r.phase, r.version, r.arm, esc(r.utc), r.R == null ? esc(r.symbol) : `<a href="#" onclick="show('${esc(r.id)}');return false">${esc(r.symbol)}</a>`, esc(r.action), esc(r.setup), esc(r.outcome), esc(r.exit_reason), r.R == null ? '' : `<span class="${r.R > 0 ? 'ok' : 'bad'}">${sgn(r.R)}</span>`, r.R_hands_off == null ? '' : sgn(r.R_hands_off), r.rr ?? '', r.wakes ?? '', `<span class="small">${esc(r.reason)}</span>`]));
  $('errors').textContent = d.errors.map(e => e.where + ': ' + e.error).join(' | ') || 'none';
}
async function show(id){
  const d = await (await fetch('/api/bc/trade/' + encodeURIComponent(id), {cache:'no-store'})).json();
  if (d.error) return;
  const W = 900, H = 240, all = d.bars.flatMap(b => [b[2], b[3]]).concat([d.sl, d.tp]), lo = Math.min(...all), hi = Math.max(...all), y = v => (H - 6 - (v - lo) / (hi - lo) * (H - 12)).toFixed(1), n = d.bars.length, x = i => (i / Math.max(n - 1, 1) * W).toFixed(1);
  const candles = d.bars.map((b, i) => `<line x1="${x(i)}" x2="${x(i)}" y1="${y(b[2])}" y2="${y(b[3])}" stroke="${b[4] >= b[1] ? 'var(--ok)' : 'var(--bad)'}"/>`).join('');
  const lvl = (v, c) => `<line x1="0" x2="${W}" y1="${y(v)}" y2="${y(v)}" stroke="${c}" stroke-dasharray="4 3"/>`, ex = d.entry_bar + (d.bars_held || 0);
  $('tradechart').innerHTML = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">${candles}${lvl(d.sl, 'var(--bad)')}${lvl(d.tp, 'var(--ok)')}${lvl(d.entry, 'var(--mut)')}<line x1="${x(d.entry_bar)}" x2="${x(d.entry_bar)}" y1="0" y2="${H}" stroke="var(--mut)"/><line x1="${x(ex)}" x2="${x(ex)}" y1="0" y2="${H}" stroke="var(--warn)"/></svg>`;
  $('tradeid').textContent = `${d.symbol} ${d.action}, ${d.utc}: ${d.exit_reason} at ${d.exit}, ${sgn(d.R)}R net ($${d.net_usd}; swap $${d.swap_usd}, commission $${d.commission_usd}), hands-off ${sgn(d.R_hands_off)}R, levels outcome ${d.outcome}`;
  $('tradeev').innerHTML = '<p>' + esc(d.reason) + '</p>' + (d.events || []).map(e => `<div>${esc(e.utc)} ${esc(e.type)} ${esc((e.triggers || []).join(','))} ${esc(e.action || '')} ${e.sl != null ? 'sl ' + e.sl : ''} ${e.tp != null ? 'tp ' + e.tp : ''} <span class="small">${esc(e.reason || e.refused || '')}</span></div>`).join('');
  $('tradecard').style.display = 'block'; $('tradecard').scrollIntoView();
}
async function act(a){ $('msg').textContent = 'working...'; const r = await (await fetch('/api/bc/' + a, {method:'POST'})).json(); $('msg').textContent = r.ok ? '' : 'refused: ' + r.reason; tick(); }
function kill(){ if (confirm('Kill stops BLINDCHEAT2 now and blocks Resume until the operator removes state/blindcheat2/KILLED. Its data stays. The assembly is not touched. Continue?')) act('kill'); }
tick(); setInterval(tick, 10000);
</script></body></html>"""

_ACTIONS = {"/api/bc/pause": do_bc_pause, "/api/bc/resume": do_bc_resume, "/api/bc/kill": do_bc_kill,
            "/api/pause": do_pause, "/api/resume": do_resume, "/api/kill": do_kill,
            "/api/mode/off": do_mode_off, "/api/mode/training": do_mode_training, "/api/mode/live": do_mode_live}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        action = _ACTIONS.get(self.path)
        if action is None:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(action()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/status"):
            body = json.dumps(status(), default=str).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/board"):
            body = json.dumps(board_status(), default=str).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/bc/trade/"):
            import urllib.parse
            body = json.dumps(_bc().trade_detail(urllib.parse.unquote(self.path[len("/api/bc/trade/"):]))).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/bc"):
            body = json.dumps(bc_status()).encode()
            ctype = "application/json"
        elif self.path.startswith("/blindcheat"):
            body = BC_PAGE.encode()
            ctype = "text/html; charset=utf-8"
        elif self.path.startswith("/api/live/run/"):
            body = json.dumps(live_run_detail(self.path.rsplit("/", 1)[-1].split("?")[0]), default=str).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/live"):
            body = json.dumps(live_status(), default=str).encode()
            ctype = "application/json"
        elif self.path in ("/live", "/live/"):
            body = LIVE_PAGE.encode()
            ctype = "text/html; charset=utf-8"
        elif self.path.startswith("/live/run/") and self.path.endswith(".md"):
            rid = self.path[len("/live/run/"):-3]
            md = HOME / "runs" / f"{rid}.md"
            ok = _is_live_id(rid) and md.exists()
            body = md.read_bytes() if ok else b"not a live call\n"
            ctype = "text/plain; charset=utf-8"
        elif self.path in ("/", "/index.html"):
            body = PAGE.encode()
            ctype = "text/html; charset=utf-8"
        elif self.path == "/board":
            body = BOARD_PAGE.encode()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 (keep the base signature)
        return


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args(argv)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"monitor on http://127.0.0.1:{a.port}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
