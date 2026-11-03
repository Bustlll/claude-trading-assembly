"""FTMO FIELD DRIVER: the loop that keeps the agent self-calling.

  python3 driver.py guard-write            record the guard hashes (bootstrap, or after a deliberate guard change)
  python3 driver.py research               queue the field research topics and run them (real Claude, costs money)
  python3 driver.py field                  turn finished research into field/*.md and field/INDEX.md
  python3 driver.py turn [--bars 288]       one turn: verify guard, run the replay to the next decision point
                                           (at most --bars M5 bars), route what happened there, drain
  python3 driver.py run --turns N          N turns, stopping early if the replay ends or the engine halts
  python3 driver.py status                 account, goals, engine ledger
  python3 driver.py live-tick              one LIVE tick (MODE must be LIVE): snapshot, events, drain
  python3 driver.py live-run               the LIVE loop (what ftmo-live.service runs); exits when MODE leaves LIVE
  python3 driver.py live-status            the live queue, today's live spend, the loop's own state

Three modes (state/MODE, the dashboard master switch): TRAINING is the historical replay -- every
turn the replay moves forward -> each routed event becomes a demand of some kind -> the reassess
demand decides what to do now -> the engine drains the queue, including every follow-up the answers
spawn, inside the caps in guard/policy.py. LIVE is the real account on the wall clock: each tick
tools/live_loop.py reads the broker and reports what happened, tools/live_routing.py turns that into
demands of the LIVE kinds (guard/policy.py LIVE_KINDS), on their own queue (state/live_selfcall.db).
OFF runs neither.
"""
from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(__file__).resolve().parent
sys.path.insert(0, str(HOME))
sys.path.insert(0, str(HOME / "tools"))
sys.path.insert(0, str(HOME.parent / "selfcall"))

from guard import policy  # noqa: E402
from selfcall.engine import Engine  # noqa: E402
import paper as P  # noqa: E402
import codetool  # noqa: E402

LEGACY_FTMO_RULES = Path.home().joinpath("selfcall/runs/325fefa6fab6.json")

TOPICS = {
    "ftmo_rules": "legacy",   # the first research answer, produced by the selfcall program earlier
    "ftmo_rules_gaps": (
        "The research answer below lists UNVERIFIED items about the FTMO challenge rules. For each UNVERIFIED item, "
        "find the official ftmo.com page that settles it (trading objectives, FAQ, rules pages). Answer each item with "
        "the exact wording and a URL, or keep it UNVERIFIED and say what you searched. Do not repeat verified items.\n\n"
        "EARLIER ANSWER:\n{legacy_summary}\n{legacy_unverified}"),
    "instruments_costs": (
        "For EURUSD on FTMO challenge accounts (MT5), find: contract size, pip value in USD per lot, typical spread in pips, "
        "commission per lot (and whether it is round turn or per side), swap rates if published, and any minimum lot step. "
        "Use ftmo.com's own pages first. Quote numbers and URLs. Mark anything not on an official page UNVERIFIED."),
    "candles_and_clock": (
        "Explain, with sources where possible: how H1 OHLC candles are built (open time, close time), bid versus ask in "
        "MT5, the difference between broker server time and UTC for FTMO MT5 servers, daylight saving effects, the "
        "Prague (CE(S)T) midnight boundary, and the weekly close and Sunday reopen. Say which of these an H1 candle file "
        "stamped in broker time would need converted before use."),
    "sizing_and_risk_math": (
        "Give the exact formulas and one worked example each for: lot size from risk percent and stop distance for a "
        "USD-quoted pair; pip value; worst-case stacking of open stops against a daily loss limit; drawdown recovery "
        "(the gain needed after a loss); and a compounding ladder where each rung is a fraction of starting equity. "
        "Use sources for the definitions where available. Show the arithmetic."),
    "measuring_a_process": (
        "How should a discretionary or semi-systematic trader measure whether their process has edge? Cover: expectancy in "
        "R multiples, the sample size needed before a win rate or expectancy means anything, standard error of an average R, "
        "why a small number of trades cannot separate skill from luck, the cost of spread and commission expressed in R, "
        "and forward (paper) testing versus backtesting. Cite sources for each claim."),
}

def load_router():
    spec = importlib.util.spec_from_file_location("routing", HOME / "agent_code" / "routing.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.route


def make_engine(invoker=None) -> Engine:
    """The TRAINING engine. Its calls go through failover_invoker(scope="training"): with two castl
    accounts and `castl auto training on` (the default) a usage limit moves training to the other
    account; `castl auto training off` keeps it on whatever account its process starts with."""
    eng = Engine(home=HOME, kinds=policy.KINDS, router=load_router(), limits=policy.LIMITS, invoker=invoker)
    if invoker is None:
        eng.invoker = failover_invoker(eng.invoker, scope="training")
    eng.child_view = train_child_view      # a spawned symbol trader starts with its symbol file
    return eng


def guard_write() -> dict:
    manifest = {"hashes": codetool.guard_hashes(), "written": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (HOME / "guard" / "MANIFEST.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def verify_guard():
    ok, msg = codetool.guard_check()
    if not ok:
        raise SystemExit(f"REFUSING TO TRADE: {msg}. Restore the guard or run guard-write deliberately.")


def _field_jobs_path() -> Path:
    return HOME / "state" / "field_jobs.json"


def _load_jobs() -> dict:
    p = _field_jobs_path()
    return json.loads(p.read_text()) if p.exists() else {}


def _save_jobs(jobs: dict):
    _field_jobs_path().write_text(json.dumps(jobs, indent=1))


def _legacy_prompt_parts():
    rec = json.loads(LEGACY_FTMO_RULES.read_text())
    ans = rec["answer"]
    unverified = [f for f in ans.get("findings", []) if "UNVERIFIED" in f.upper()]
    return ans.get("summary", ""), "\n".join(f"- {u}" for u in unverified)


def research() -> dict:
    """Queue every field topic that is not queued yet, then run the queue."""
    eng = make_engine()
    jobs = _load_jobs()
    if "ftmo_rules" not in jobs and LEGACY_FTMO_RULES.exists():
        jobs["ftmo_rules"] = {"legacy": str(LEGACY_FTMO_RULES)}
    summary, unverified = _legacy_prompt_parts() if LEGACY_FTMO_RULES.exists() else ("", "")
    for topic, prompt in TOPICS.items():
        if topic in jobs or prompt == "legacy":
            continue
        text = prompt.format(legacy_summary=summary, legacy_unverified=unverified) \
            if "{legacy_summary}" in prompt else prompt
        jobs[topic] = {"demand": eng.enqueue("research", text, source=f"bootstrap:field:{topic}")}
    _save_jobs(jobs)
    what, detail = eng.drain()
    return {"drain": what, "detail": detail, "topics": {t: j for t, j in jobs.items()}}


def _record_for(job: dict) -> dict:
    if "legacy" in job:
        return json.loads(Path(job["legacy"]).read_text())
    return json.loads((HOME / "runs" / f"{job['demand']}.json").read_text())


def field() -> str:
    jobs = _load_jobs()
    index = ["# FIELD INDEX", "",
             "Researched by the selfcall program (research kind, real web sources). Every item below carries its sources.",
             "Items marked UNVERIFIED are assumptions until a source settles them. Treat them as assumptions in your",
             "reasoning and say so in your journal.", ""]
    built = []
    for topic, job in jobs.items():
        try:
            rec = _record_for(job)
        except FileNotFoundError:
            index.append(f"- {topic}: not finished yet")
            continue
        ans = rec.get("answer") or {}
        if rec.get("status") not in ("done", "blocked") or not ans:
            index.append(f"- {topic}: {rec.get('status')} ({rec.get('error', '')[:80]})")
            continue
        lines = [f"# {topic}", "", f"status: {rec['status']} · cost ${rec.get('cost_usd') or 0:.3f} · run {rec['id']}",
                 "", "## Summary", ans.get("summary", ""), ""]
        if ans.get("findings"):
            lines += ["## Findings"] + [f"- {f}" for f in ans["findings"]] + [""]
        if ans.get("sources"):
            lines += ["## Sources"] + [f"- {s}" for s in ans["sources"]] + [""]
        (HOME / "field" / f"{topic}.md").write_text("\n".join(lines))
        head = (ans.get("summary") or "").split(". ")[0][:160]
        index.append(f"- [{topic}]({topic}.md): {head}")
        built.append(topic)
    # measured notes (not web research): listed whenever their file exists, so a rebuild keeps them
    for topic, head in MEASURED_FIELD_NOTES:
        if (HOME / "field" / f"{topic}.md").exists():
            index.append(f"- [{topic}]({topic}.md): {head}")
    (HOME / "field" / "INDEX.md").write_text("\n".join(index) + "\n")
    return f"field built: {', '.join(built) or 'nothing finished yet'}"


MEASURED_FIELD_NOTES = [
    ("measured_spreads", "Each instrument's typical spread, its busy-hours spread and its rollover spike, measured "
                         "from the broker's own bars -- and what that costs in R"),
]


def drain_outbox() -> list[dict]:
    path = P.HOME / "state" / "outbox.jsonl"
    if not path.exists():
        return []
    with open(path) as f:
        events = [json.loads(line) for line in f if line.strip()]
    path.write_text("")
    return events


def mode() -> str:
    """OFF/TRAINING/LIVE master switch (state/MODE, written by the dashboard -- see
    tools/monitor_ui.py read_mode/do_set_mode and manual/LIVE_TRADING_IDEA.md). Checked here too,
    not only in the dashboard's own start/stop calls: defense in depth, same as this project's
    other guard checks -- a service started by hand while MODE says otherwise must not silently
    advance the paper replay."""
    p = HOME / "state" / "MODE"
    m = p.read_text().strip() if p.exists() else "TRAINING"
    return m if m in ("OFF", "TRAINING", "LIVE") else "TRAINING"


_PAPER = None                       # the run loop's one Paper: feeds load once (~7 s), state reloads each turn
RECENT_PATH = HOME / "state" / "train_recent_events.json"
RECENT_EVENTS_MAX = 30              # live_loop RECENT_EVENTS_MAX
_BRIEF_TEXT_MAX = 240
TRAIN_BARS_CMD = f"python3 {HOME}/tools/paper.py bars"
TRAIN_LESSONS_CMD = f"python3 {HOME}/tools/board.py lessons"
LIVE_BARS_CMD = f"python3 {HOME}/tools/live_market.py bars"
LIVE_LESSONS_CMD = f"python3 {HOME}/tools/live_board.py lessons"


def _desk():
    """tools/desk_shared.py: what a twin worker sees, built the same way for both desks (2026-10-18)."""
    if str(HOME / "tools") not in sys.path:
        sys.path.insert(0, str(HOME / "tools"))
    import desk_shared
    return desk_shared


def train_views(ev: dict, paper, snap: dict) -> dict:
    """The shared view for one training event (prompt only -- never written to the ledger, whose rows the
    reviews read back): a scan gets each mover's candles, today's board thread and the lessons on its path;
    an alert wake and a closed trade get the lessons on theirs."""
    DS = _desk()
    import board as TB
    import kstore as K
    if ev["type"] == "setup_candidate":
        syms = [c["symbol"] for c in ev.get("candidates") or []]
        return DS.scout_view("training", lambda s, tf, n: paper.recent_bars(s, n, tf),
                             lambda s: TB.read(symbol=s, n=0, home=HOME), snap.get("prague_day"), K.rec_day,
                             syms, ev.get("utc") or snap.get("utc"), TRAIN_BARS_CMD, TRAIN_LESSONS_CMD)
    if ev["type"] == "level_alert":
        return {"symbol_file": train_symbol_file(ev["symbol"], str(ev.get("why") or ""), paper)}
    if ev["type"] == "manage_wake":     # the manager reads the same symbol file a trader does, on its plan's path
        return {"symbol_file": train_symbol_file(ev["symbol"], str(ev.get("why") or ""), paper)}
    if ev["type"] == "trade_closed":
        return {"lessons_text": DS.review_lessons("training", ev, TRAIN_LESSONS_CMD)}
    if ev["type"] == "day_closed":
        import opportunities as OPP
        n, text = OPP.day_review("training", K.visible_ledger(HOME), TB.read(n=0, home=HOME), ev.get("day"), K.rec_day,
                                 lambda s, tf, k: paper.recent_bars(s, k, tf), HOME)
        return {"graded_n": n, "graded_text": text} if text else {}
    return {}


def _paper() -> "P.Paper":
    global _PAPER
    if _PAPER is None:
        _PAPER = P.Paper()
    else:
        _PAPER.reload()
    return _PAPER


def _brief(ev: dict) -> dict:
    """What the next decision turn needs to know about an event since the last one, not its whole
    record: long texts are cut, the bar index dropped (the utc stays)."""
    out = {}
    for k, v in ev.items():
        if k == "bar":
            continue
        out[k] = (v[:_BRIEF_TEXT_MAX] + "...") if isinstance(v, str) and len(v) > _BRIEF_TEXT_MAX else v
    return out


def _load_recent() -> list[dict]:
    try:
        return json.loads(RECENT_PATH.read_text())
    except (OSError, ValueError):
        return []


DREAMS_INLINE = 4


def dreams_text(n: int = DREAMS_INLINE) -> str:
    """The newest dreams, in full, for the turns that decide an entry (2026-11-01: on the 11-01 replay no worker ran
    `dreams.py read` once, so a dream reached nobody; the paragraph pointing at the command cost every prompt)."""
    try:
        import dreams as DR
        ds = DR.dreams(HOME)
    except Exception:
        return ""
    newest = sorted(ds.items(), key=lambda kv: int(kv[0].split()[1]) if kv[0].split()[1].isdigit() else 0)[-n:]
    if not newest:
        return ""
    return (f"DREAMS (the newest {len(newest)} of {len(ds)}; process hypotheses of unknown origin -- test them against "
            "your own record, which wins):\n" + "\n".join(f"- {k}: {DR._rule(v)}" for k, v in newest))


DECIDER_PACKET = (("M15", 16), ("H1", 12))   # the top movers' candles, attached to the decision turn
DECIDER_PACKET_SYMBOLS = 3
BOARD_DIGEST_POSTS = 12
BOARD_DIGEST_CHARS = 260


def decider_attachments(paper, snap: dict, movers: list[dict]) -> dict:
    """What the decision turn used to fetch itself, attached (2026-11-01: per decision turn on the 11-01 replay, 1.1 `bars`
    calls and 1.0 `board read` calls -- each a round trip of the whole prompt). The top movers' M15/H1 candles, and today's
    board posts (newest last, each cut to a line)."""
    DS = _desk()
    import board as TB
    import kstore as K
    syms = [m["symbol"] for m in DS.collapse_twins([m for m in movers if abs(m.get("pct_move") or 0) >= P.SCAN_THRESHOLD_PCT])
            ][:DECIDER_PACKET_SYMBOLS]
    out = {}
    if syms:
        out["movers_bars"] = ("THE TOP MOVERS' CANDLES (closed bars, the table `paper.py bars` prints; utc_open session "
                              "o h l c):\n" + "\n".join(DS.packet(lambda s, tf, n: paper.recent_bars(s, n, tf), s,
                                                                    DECIDER_PACKET) for s in syms))
    day = snap.get("prague_day")
    posts = [q for q in TB.read(n=0, home=HOME) if K.rec_day(q) == day and "lesson" not in str(q.get("topic"))
             and q.get("topic") != "risk"][-BOARD_DIGEST_POSTS:]   # the lessons reach it on their own path
    if posts:
        lines = TB.read_text(posts).splitlines()
        out["board_text"] = ("TODAY ON THE BOARD (the last posts, each cut to one line; board read --symbol S has them "
                             "in full):\n" + "\n".join(x if len(x) <= BOARD_DIGEST_CHARS else x[:BOARD_DIGEST_CHARS] + "..."
                                                       for x in lines))
    return out


def turn(bars: int) -> dict:
    """One turn = the replay runs to the NEXT DECISION POINT (tools/paper.py THE DECISION CLOCK), at
    most `bars` M5 bars, then routes what happened there. Before 2026-10-18 a turn was always a whole
    day (288 bars) routed at its end, so every hourly scout looked at a move hours old."""
    verify_guard()
    m = mode()
    if m != "TRAINING":
        # OFF: nothing runs. LIVE: the replay must not advance either -- the live loop is a
        # different program (live-run below, ftmo-live.service), and a replay fill while the
        # dashboard says LIVE would be the "paper fill mislabeled as real" confusion
        # manual/LIVE_TRADING_IDEA.md rules out.
        detail = (f"mode is {m}, not TRAINING: the replay does not advance"
                  + ("; the live loop is `driver.py live-run`" if m == "LIVE" else ""))
        return {"drain": "halted", "detail": detail, "advanced": None, "events": [], "bar": None, "equity": None}
    import castigation as CG
    cstop = CG.stopped(HOME)     # the training-only castigation stop (never STOP: the live loop shares that one)
    if cstop:
        return {"drain": "halted", "detail": cstop, "advanced": None, "events": [], "bar": None, "equity": None}
    eng = make_engine()
    # finish every decision still owed before the replay moves. A limit leaves it queued; then we do not advance.
    what, detail = eng.drain()
    if what in ("limited", "halted"):
        return {"drain": what, "detail": detail, "advanced": None, "events": [], "bar": None, "equity": None}
    paper = _paper()
    adv = paper.advance_to_decision(bars)  # attempts auto-advance through windows and wrap epochs on their own
    events = drain_outbox()
    snap = paper.snapshot()
    import market_feed
    market_feed.distribute("TRAINING", snapshot=snap)  # publish once; see manual/LIVE_TRADING_IDEA.md
    goals = paper.goals()
    # the desk's own context (2026-11-01, operator: "it needs to be aware of its own context"; tools/castigation.py):
    # what it has cost since its last order rides in every prompt, and a castigated desk's scouts are muted.
    # TRAINING ONLY: live_tick never measures, mutes or pushes anything for usage.
    cg = CG.meter(HOME, sim_utc=snap.get("now_utc") or snap.get("utc"))
    own = {"self_text": CG.self_text(cg), "scout_text": CG.self_text(cg, scout=True), "castigated": cg.get("state") == "castigated",
           "castigation_text": CG.castigation_text(cg) if cg.get("state") == "castigated" else "",
           "dreams_text": dreams_text()}
    # emit() can refuse a demand (e.g. too long); one bad event must not take the whole service down.
    skipped = []

    def safe_emit(event, payload):
        try:
            return eng.emit(event, payload)
        except ValueError as exc:
            skipped.append({"event": event, "reason": str(exc)})
            return None

    # everything since the last DECISION turn rides along to the next one (scan/alert stops route their
    # own events at once, and the decision turn still sees them), the way live_loop's recent_events does
    recent = (_load_recent() + [_brief(e) for e in events])[-RECENT_EVENTS_MAX:]
    if adv["stop"] == "reassess":
        # reassess first so the decision runs before the reviews of what just happened.
        movers = adv.get("movers") or []
        safe_emit("turn_reassess", {"snapshot": snap, "goals": goals, "recent_events": recent,
                                    "movers": movers, "window": P.train_window_text(),
                                    "next_decision_utc": adv.get("next_decision_utc"), **own,
                                    **_view(lambda ev: decider_attachments(paper, snap, movers), {}),
                                    "lessons_text": _desk().decider_lessons("training", snap.get("positions") or [],
                                                                            movers, snap.get("utc"),
                                                                            TRAIN_LESSONS_CMD)})
        recent = []
    if adv["stop"] == "swing":
        # the once-a-day swing turn (2026-10-20): the same view live_views builds for live_swing
        movers = adv.get("movers") or []
        sv = _view(lambda ev: _desk().swing_view(lambda s, tf, n: paper.recent_bars(s, n, tf), movers, TRAIN_BARS_CMD), {})
        safe_emit("turn_swing", {"snapshot": snap, "goals": goals, "movers": movers, "recent_events": recent, **sv, **own,
                                 "lessons_text": _desk().decider_lessons("training", snap.get("positions") or [], movers,
                                                                         snap.get("utc"), TRAIN_LESSONS_CMD)})
    RECENT_PATH.write_text(json.dumps(recent))
    routed = []
    for ev in events:
        payload = {k: v for k, v in ev.items() if k != "type"}
        payload["goals"] = goals
        payload.update(own)
        # what the woken worker would read first, already in its prompt (tools/desk_shared.py, the same view
        # the live desk builds): candles, the symbol's thread today, the lessons on its path
        payload.update(_view(train_views, ev, paper, snap))
        did = safe_emit(ev["type"], payload)
        routed.append({"event": ev["type"], "demand": did})
    import kstore as _K
    for ev in [e for e in events if e["type"] == "day_closed"]:      # the vault's daily value extraction (2026-10-20)
        did = safe_emit("vault_review", {"day": ev.get("day"),
                                         **_view(lambda e: {"vault_text": vault_paperwork("training", e.get("day"))}, ev)})
        routed.append({"event": "vault_review", "demand": did})
    for ev in calibration_due("training", [e.get("day") for e in events if e["type"] == "day_closed"], _K.now_utc(HOME)):
        did = safe_emit(ev["type"], {k: v for k, v in ev.items() if k != "type"})
        routed.append({"event": ev["type"], "demand": did})
    if skipped:
        with open(HOME / "state" / "skipped_events.jsonl", "a") as f:
            for s in skipped:
                f.write(json.dumps({"bar": snap["bar_index"], "utc": snap["utc"], **s}) + "\n")
    what, detail = eng.drain()

    def _castigate(m):     # once, when the desk crosses CASTIGATE_PCT without an order: into its own record
        import board as TB
        CG.record_lesson(m, lambda w, t, x: TB.post(w, t, x, home=HOME))
        paper.reload()
        paper.log("castigation", pct=m["pct"], usd=m["usd"], calls=m["calls"], sim_since=m.get("sim_since"))
        paper.save()
    cg = CG.settle(HOME, sim_utc=snap.get("now_utc") or snap.get("utc"), on_castigate=_castigate)
    gate = code_gate(eng, paper)
    if gate and gate.get("gate") == "repair_queued" and what not in ("limited", "halted"):
        what, detail = eng.drain()          # the repair runs before the replay moves on
        gate = code_gate(eng, paper) or gate  # and is checked (or reverted) at once
    result = {"at": snap["utc"], "bar": snap["bar_index"], "equity": snap["equity"], "stop": adv["stop"],
              "code_gate": gate,
              "castigation": {k: cg.get(k) for k in ("state", "pct", "usd", "calls", "changed")},
              "advanced": adv, "events": routed, "drain": what, "detail": detail}
    with open(HOME / "state" / "turns.jsonl", "a") as f:
        f.write(json.dumps(result) + "\n")
    import fills_view
    result["fills_view"] = fills_view.build()
    return result


# ---- CODE GATE: an evolve never leaves broken code behind ----------------------------------------
CODE_GATE = HOME / "state" / "code_gate.json"


def _selftest() -> tuple[bool, str]:
    proc = subprocess.run([sys.executable, str(HOME / "tools" / "codetool.py"), "selftest"],
                          capture_output=True, text=True, cwd=HOME, timeout=900)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def _evolve_backups(t0: float, t1: float) -> dict[str, str]:
    """{path under HOME: earliest backup taken in [t0, t1]} -- codetool backup names a copy <file>.<stamp>.bak,
    so the original is found by its file name under agent_code/ (versions/ excluded)."""
    found: dict[str, str] = {}
    for b in sorted((HOME / "agent_code" / "versions").glob("*.bak"), key=lambda p: p.stat().st_mtime):
        if not (t0 - 5 <= b.stat().st_mtime <= t1 + 5):
            continue
        name = b.name.rsplit(".", 2)[0]
        hits = [p for p in (HOME / "agent_code").rglob(name) if "versions" not in p.parts]
        if len(hits) == 1:
            found.setdefault(str(hits[0].relative_to(HOME)), str(b))
    return found


def _diff(backups: dict[str, str], limit: int = 7000) -> str:
    out = []
    for rel, bak in backups.items():
        a = Path(bak).read_text(errors="ignore").splitlines()
        b = (HOME / rel).read_text(errors="ignore").splitlines() if (HOME / rel).exists() else []
        out += list(difflib.unified_diff(a, b, f"{rel} (before)", f"{rel} (after)", lineterm="", n=2))
    text = "\n".join(out)
    return text if len(text) <= limit else text[:limit] + "\n... (diff cut)"


def _gate_record(paper, note: str):
    try:
        paper.log("code_gate", note=note)
        import board as BOARD
        BOARD.post("review", "code_gate", note)
    except Exception:
        pass


def code_gate(eng, paper) -> dict | None:
    """After every evolve (the agent changing its own code): the self-test, in code, for free. Broken ->
    ONE repair demand with the failures and the diff, told to keep the upgrade. Still broken once that repair
    is done -> the pre-evolve backups are restored and the board is told why. One repair per evolve, at most:
    a broken upgrade cannot stack up behind training. Returns what it did, or None."""
    st = json.loads(CODE_GATE.read_text()) if CODE_GATE.exists() else {"checked_until": time.time()}
    db = eng.store.db
    pend = st.get("pending")
    if pend:
        row = db.execute("SELECT status FROM demands WHERE id=?", (pend["repair"],)).fetchone()
        if row is not None and row["status"] in ("queued", "running"):
            return None
        ok, out = _selftest()
        if ok:
            note = f"code gate: repair {pend['repair']} fixed what evolve {pend['evolve']} broke; self-test green"
        else:
            import codetool as CT
            fails = "; ".join(sum((CT.failures(x) for x in [out]), [])) or out[-300:]
            stamp = time.strftime("%Y%m%dT%H%M%S")
            for rel, bak in pend["backups"].items():
                cur = HOME / rel
                if cur.exists():
                    shutil.copy2(cur, HOME / "agent_code" / "versions" / f"{cur.name}.{stamp}.broken.bak")
                shutil.copy2(bak, cur)
            ok2, _ = _selftest()
            note = (f"code gate: evolve {pend['evolve']} REVERTED ({', '.join(pend['backups']) or 'no backups found'}) "
                    f"-- the self-test still failed after repair {pend['repair']}: {fails[:600]}. After the revert the "
                    f"self-test is {'green' if ok2 else 'STILL FAILING (operator: look)'}. The idea was sound enough to "
                    "try; re-propose it as a smaller change with its tests.")
        st.pop("pending")
        CODE_GATE.write_text(json.dumps(st))
        _gate_record(paper, note)
        return {"gate": "closed", "note": note}
    rows = db.execute("SELECT id, started, finished, answer FROM demands WHERE kind='evolve' AND finished > ? "
                      "AND status IN ('done','blocked','failed') ORDER BY finished", (st["checked_until"],)).fetchall()
    if not rows:
        if not CODE_GATE.exists():
            CODE_GATE.write_text(json.dumps(st))
        return None
    st["checked_until"] = max(r["finished"] for r in rows)
    ok, out = _selftest()
    if ok:
        CODE_GATE.write_text(json.dumps(st))
        return None
    ev = rows[-1]
    backups = _evolve_backups(min(r["started"] for r in rows), st["checked_until"])
    try:
        summary = (json.loads(ev["answer"] or "{}").get("summary") or "")[:1200]
    except ValueError:
        summary = ""
    import codetool as CT
    fails = CT.failures(out) or out.splitlines()[-12:]
    prompt = ("CODE GATE: the self-test fails right after an evolve changed the agent's own code. Make it pass "
              "again WITHOUT losing what the evolve meant to add.\n\n"
              f"WHAT THE EVOLVE SAID IT DID:\n{summary}\n\nFAILING TESTS:\n" + "\n".join(fails) + "\n\n"
              f"ITS CHANGES (before -> after):\n{_diff(backups) or '(no backups found for this evolve)'}\n\n"
              "HOW: if a test still expects what the upgrade deliberately changed, update the test; if the new code "
              "is wrong, fix the code. Back up any file before you edit it (python3 " + str(HOME / "tools" / "codetool.py") +
              " backup <path under agent_code/>), run python3 " + str(HOME / "tools" / "codetool.py") + " selftest "
              "before you answer, and say exactly what you changed and why. Do not revert the upgrade wholesale: if "
              "you cannot fix it, say so -- the program then restores the pre-evolve files itself.")
    did = eng.enqueue("repair", prompt[:19_000], source=f"code_gate:{ev['id']}")
    st["pending"] = {"evolve": ev["id"], "repair": did, "backups": backups}
    CODE_GATE.write_text(json.dumps(st))
    _gate_record(paper, f"code gate: evolve {ev['id']} broke the self-test ({len(fails)} failing); repair {did} queued")
    return {"gate": "repair_queued", "repair": did}


def status() -> dict:
    paper = P.Paper()
    eng = make_engine()
    n, cost = eng.store.totals()
    return {"account": paper.snapshot(), "goals": paper.goals(), "engine_halt": eng.halt_reason() or "none",
            "invocations": n, "cost_usd": round(cost, 4), "counts": eng.store.counts()}


# ---- LIVE --------------------------------------------------------------------------------------
DELTA_ERRORS_FILE = Path(os.environ.get(
    "FTMO_BLANK_DELTA_ERRORS_FILE", str(Path.home()) + "/deltaForceFTMO/deltaforce/data/runtime_state/errors.jsonl"))
LIVE_DEMAND_TTL_S = 55 * 60        # still queued after this: a newer decision turn already supersedes it
LIVE_WEEKEND_SLEEP_S = 1800
CASTL_HOME = Path(os.environ.get("FTMO_BLANK_CASTL_HOME", str(Path.home() / "claude_account_switch")))
DEFAULT_CLAUDE_DIR = Path.home() / ".claude"


def load_live_router():
    import live_routing
    return live_routing.route


def _account_store():
    """castl's account registry (~/.castl/accounts.json, ~/claude_account_switch) -- the same file the
    castl CLI reads and writes, so an account the loop finds exhausted is exhausted for castl too.
    None (no switching, today's behaviour) unless castl is present and at least TWO accounts are
    registered: switching to the account you are already on would only hide a limit."""
    try:
        if str(CASTL_HOME) not in sys.path:
            sys.path.insert(0, str(CASTL_HOME))
        from castl_lib.state import AccountStore
        store = AccountStore()
        return store if len(store.all()) >= 2 else None
    except Exception:
        return None


def _use_account(store, name: str) -> None:
    """Point the next `claude -p` (a child of this process, which inherits os.environ) at one account.
    The default account is selected by UNSETTING the variable, exactly as every call ran before, rather
    than by naming ~/.claude explicitly."""
    cfg = Path(store.require(name)["config_dir"]).expanduser()
    if hasattr(store, "set_active"):
        store.set_active(name)  # sticky: the next call stays here until this account hits a limit
    if cfg.resolve() == DEFAULT_CLAUDE_DIR.resolve():
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
    else:
        os.environ["CLAUDE_CONFIG_DIR"] = str(cfg)


def failover_invoker(base, store_factory=None, scope="live"):
    """Wrap the engine's invoker: run each call on the account in use (castl's active one) while it is
    available; on a usage limit, mark that account exhausted until its stated reset and retry the SAME
    call on the next one at once. Only when every account is waiting does the limit reach the engine
    (which then waits as it always has, with the earliest reset in the message so it does not guess),
    and the account back first is the one used next. A pass-through with fewer than two accounts
    registered, or when castl's autoswitch for `scope` ("live" / "training") is off."""
    from selfcall.engine import LimitHit, parse_reset_wait
    store_factory = store_factory or _account_store

    def run(d):
        store = store_factory()
        if store is None:
            return base(d)
        try:
            switching = store.get_auto(scope) if hasattr(store, "get_auto") else True
        except Exception:
            switching = True
        if not switching:
            return base(d)
        tried: set = set()
        while True:
            store.reload()
            name = store.pick_available(exclude=tried)
            if name is None:
                at = store.earliest_available_at()
                from datetime import datetime
                from zoneinfo import ZoneInfo
                when = (datetime.fromtimestamp(at, ZoneInfo("Europe/Madrid")).strftime("%I:%M%p").lstrip("0").lower()
                        if at else None)
                raise LimitHit("every registered Claude account is waiting out a usage limit"
                               + (f"; the first resets {when} (Europe/Madrid)" if when else ""))
            tried.add(name)
            _use_account(store, name)
            try:
                return base(d)
            except LimitHit as exc:
                wait = parse_reset_wait(str(exc))
                if wait is None and str(exc).startswith("account down"):
                    wait = 6 * 3600    # 2026-11-02: billing/subscription/login off (selfcall's "account down"); re-tried every 6h
                store.mark_exhausted(name, time.time() + (wait if wait is not None else 1800))
    return run


def make_live_engine(invoker=None) -> Engine:
    """The LIVE engine: LIVE_KINDS, the operator-owned live router, LIVE_LIMITS, and its OWN queue
    (state/live_selfcall.db) so live calls are never mixed with the replay's. Shares state/STOP with
    training on purpose: the dashboard's Pause and Kill stop both. Its calls go through
    failover_invoker, so a usage limit on one account moves the live loop to the other."""
    from selfcall.engine import Store
    eng = Engine(home=HOME, kinds=policy.LIVE_KINDS, router=load_live_router(), limits=policy.LIVE_LIMITS,
                 invoker=invoker)
    if invoker is None:
        eng.invoker = failover_invoker(eng.invoker, scope="live")
    eng.store = Store(HOME / "state" / "live_selfcall.db")
    eng.child_view = lambda kind, prompt, parent=None: live_child_view(kind, prompt, parent)  # live_tick passes its m
    return eng


def _prague_midnight_ts() -> float:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Europe/Prague"))
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def live_spent_today(eng: Engine) -> float:
    row = eng.store.db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM demands WHERE started >= ?",
                               (_prague_midnight_ts(),)).fetchone()
    return float(row[0])


def live_calls_today(eng: Engine) -> int:
    row = eng.store.db.execute("SELECT COUNT(*) FROM demands WHERE started >= ?", (_prague_midnight_ts(),)).fetchone()
    return int(row[0])


def expire_stale_live_demands(eng: Engine, now_ts: float | None = None) -> int:
    now_ts = now_ts or time.time()
    cur = eng.store.db.execute(
        "UPDATE demands SET status='failed', finished=?, error=? WHERE status='queued' AND created < ?",
        (now_ts, f"expired: still queued after {LIVE_DEMAND_TTL_S // 3600}h -- the market it was about has "
                 "moved on (usually a usage-limit wait); the next tick decides afresh", now_ts - LIVE_DEMAND_TTL_S))
    eng.store.db.commit()
    return cur.rowcount


def runaway_alert(calls: int, cap: int) -> dict:
    """The runaway guard just stopped a call: say so LOUDLY, once per Prague day (operator, 2026-10-11:
    "make sure the errors pop up when calls surpass it"). Lands in three places:
      * the deltaForceFTMO dashboard's Errors tab -- a row in its errors.jsonl, the same shape
        deltaforce.observability.log_error writes (component ftmo_blank.assembly, level error);
      * this project's dashboard (state/live_alert.json -> a red line on the mode card);
      * state/live_errors.jsonl and the live loop's own log."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    day = datetime.now(ZoneInfo("Europe/Prague")).date().isoformat()
    marker = HOME / "state" / "live_alert.json"
    try:
        if json.loads(marker.read_text()).get("day") == day:
            return {"alerted": False, "reason": "already alerted today"}
    except (OSError, json.JSONDecodeError):
        pass
    msg = (f"RUNAWAY GUARD TRIPPED: the live assembly made {calls} Claude calls today (cap {cap}); no new calls "
           "until the Prague midnight reset. A normal day is ~25-60 -- look for a spawn loop in "
           "state/live_turns.jsonl before raising guard/policy.py LIVE_DAILY_MAX_CALLS.")
    now = time.time()
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now)), "ts_epoch": now,
           "component": "ftmo_blank.assembly", "level": "error", "message": msg,
           "extra": {"calls_today": calls, "cap": cap, "day": day}}
    marker.write_text(json.dumps({"day": day, **rec}))
    with open(HOME / "state" / "live_errors.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    delta = False
    try:
        if DELTA_ERRORS_FILE.parent.is_dir():
            with open(DELTA_ERRORS_FILE, "a") as f:   # one short line: an atomic append next to its own writers
                f.write(json.dumps(rec) + "\n")
            delta = True
    except OSError:
        pass
    print("!!! " + msg, flush=True)
    return {"alerted": True, "delta_errors_tab": delta}


SYMBOL_OF_TRADER = {f"trade_{s.lower().replace('.', '_')}": s for s in policy.SYMBOL_TRADERS}
SYMBOL_OF_LIVE_TRADER = {f"live_trade_{s.lower().replace('.', '_')}": s for s in policy.SYMBOL_TRADERS}


def vault_paperwork(desk: str, day: str | None, m=None) -> str:
    """The vault worker's paperwork (tools/vault.py): rebuilt incrementally, then the code report and the day's own trades
    of this desk, each with its entry read and what happened after. Training sees real trades without dates/prices."""
    import vault as V
    V.build(m, refresh=(m is not None))
    now_sim = None
    if desk == "training":
        import kstore as K
        t = K.now_utc(HOME)
        now_sim = t.timestamp() if t else None
    rows = V.load(desk, now_sim)
    src = "assembly_live" if desk == "live" else "assembly_training"
    today = [r for r in rows if r.get("source") == src and (str(r.get("close_utc") or "")[:10] == str(day or ""))]
    lines = [V.report(rows, desk), f"THIS DESK'S TRADES CLOSED ON {day} ({len(today)}):"]
    for r in today:
        lines.append("  " + json.dumps({k: r.get(k) for k in ("symbol", "side", "open_utc", "R", "exit_reason", "plan",
                                                                 "f", "o")}, default=float)[:1500])
    return "\n".join(lines)


def train_symbol_file(symbol: str, plan_text: str, paper) -> str:
    """tools/desk_shared.py trader_view on the replay: what a training trader reads first when it wakes."""
    DS = _desk()
    import board as TB
    import kstore as K
    return DS.trader_view("training", lambda s, tf, n: paper.recent_bars(s, n, tf),
                          lambda s: TB.read(symbol=s, n=0, home=HOME), paper.s.get("day"), K.rec_day, symbol,
                          plan_text, P.bar_utc(paper.t_now()) if paper.t_now() else None,
                          K.trades(HOME, symbol=symbol, n=DS.RECORD_N)["trades"], TRAIN_BARS_CMD, TRAIN_LESSONS_CMD)


def live_symbol_file(symbol: str, plan_text: str, m=None) -> str:
    """The same trader_view on the broker's bars and the live board / live record."""
    DS = _desk()
    import live_board as LBd
    import live_broker as LB
    import live_market as LM
    from zoneinfo import ZoneInfo
    now = LB._now_utc()
    return DS.trader_view("live", lambda s, tf, n: LM.bars(s, n, tf, m=m),
                          lambda s: LBd.read(symbol=s, n=0, home=HOME),
                          now.astimezone(ZoneInfo(LBd.DAY_TZ)).date().isoformat(), lambda p: p.get("day"), symbol,
                          plan_text, now, LB.closed_trades(n=0, symbol=symbol, home=HOME)[-DS.RECORD_N:],
                          LIVE_BARS_CMD, LIVE_LESSONS_CMD)


def train_child_view(kind: str, prompt: str, parent=None) -> str | None:
    """A symbol trader spawned by a scout or the decider gets its symbol file in its context (engine
    child_view): it is the worker that executes, so it must not see less than the scout that handed it on."""
    sym = SYMBOL_OF_TRADER.get(kind)
    if not sym and kind == "trade":     # the decider handed one symbol by a scout (live_child_view's rule, same as "live")
        import pathways as PW
        from guard import rules as GR
        named = [s for s in PW.symbols_in(prompt) if s in GR.INSTRUMENTS]
        sym = named[0] if len(named) == 1 else None
    if not sym or _PAPER is None or _PAPER.t_now() is None:
        return None
    text = train_symbol_file(sym, prompt, _PAPER)
    if kind == "trade":                 # a spawned decider has no decision-turn prompt: how to act, in one line
        text += (f"\n\nTO ACT: python3 {HOME}/tools/paper.py order --symbol {sym} --side buy|sell --lots X --sl P [--tp P] --why \"CODE ...\" "
                 f"(fills at the next bar's open; lot spec: python3 {HOME}/agent_code/kstore.py spec {sym}), or paper.py alert ... at a "
                 "level still to come. Then one journal note and one board post (VERDICT line).\n" + _desk().entry_rules("training"))
    return text


def live_child_view(kind: str, prompt: str, parent=None, m=None) -> str | None:
    sym = SYMBOL_OF_LIVE_TRADER.get(kind)
    if not sym and kind == "live":          # the decider handed one symbol by a live scout (live-only path)
        import pathways as PW
        from guard import rules as GR
        named = [s for s in PW.symbols_in(prompt) if s in GR.INSTRUMENTS]
        sym = named[0] if len(named) == 1 else None
    return live_symbol_file(sym, prompt, m) if sym else None


def live_views(ev: dict, snap: dict | None, m=None) -> dict:
    """The live twin of train_views(): the same tools/desk_shared.py view, built from the broker and the
    live board. A scan gets each mover's candles (live_market.bars, closed bars only), today's live-board
    thread and the lessons on its path; a decision turn and a closed trade get the lessons on theirs."""
    DS = _desk()
    import live_board as LBd
    import live_market as LM
    from zoneinfo import ZoneInfo
    snap = snap or {}
    when = snap.get("utc")
    if ev["type"] == "live_setup_candidate":
        today = LBd._now().astimezone(ZoneInfo(LBd.DAY_TZ)).date().isoformat()
        syms = [c["symbol"] for c in ev.get("candidates") or []]
        return DS.scout_view("live", lambda s, tf, n: LM.bars(s, n, tf, m=m),
                             lambda s: LBd.read(symbol=s, n=0, home=HOME), today, lambda p: p.get("day"),
                             syms, when, LIVE_BARS_CMD, LIVE_LESSONS_CMD)
    if ev["type"] == "live_reassess":
        return {"lessons_text": DS.decider_lessons("live", snap.get("own_positions") or [], ev.get("movers") or [],
                                                   when, LIVE_LESSONS_CMD)}
    if ev["type"] == "live_trade_closed":
        return {"lessons_text": DS.review_lessons("live", ev, LIVE_LESSONS_CMD)}
    if ev["type"] == "live_level_alert":
        return {"symbol_file": live_symbol_file(ev["symbol"], str(ev.get("why") or ""), m)}
    if ev["type"] == "live_manage":
        return {"symbol_file": live_symbol_file(ev["symbol"], str(ev.get("why") or ""), m)}
    if ev["type"] == "live_vault_review":   # rebuild the vault (new trades, fresh bars), then the paperwork
        return {"vault_text": vault_paperwork("live", ev.get("day"), m)}
    if ev["type"] == "live_swing":      # the once-a-day swing turn: D1/H4/H1 of gold and the day's movers, + lessons
        return {**DS.swing_view(lambda s, tf, n: LM.bars(s, n, tf, m=m), ev.get("movers") or [], LIVE_BARS_CMD),
                "lessons_text": DS.decider_lessons("live", (snap or {}).get("own_positions") or [],
                                                   ev.get("movers") or [], when, LIVE_LESSONS_CMD)}
    if ev["type"] == "live_day_closed":
        import opportunities as OPP
        import live_broker as LB
        prague = ZoneInfo(LBd.DAY_TZ)

        def day_of(r):   # the Prague day of a live ledger row (an alert's own bar time first)
            t = OPP._t(r.get("touched_utc") or r.get("utc") or r.get("logged_utc"))
            return t.astimezone(prague).date().isoformat() if t else None
        n, text = OPP.day_review("live", LB.ledger_rows(home=HOME), LBd.read(n=0, home=HOME), ev.get("day"), day_of,
                                 lambda s, tf, k: LM.bars(s, k, tf, m=m), HOME)
        return {"graded_n": n, "graded_text": text} if text else {}
    return {}


def calibration_due(desk: str, days: list[str], upto=None) -> list[dict]:
    """Every CALIBRATE_EVERY_DAYS closed trading days, one calibration review (tools/opportunities.py): the
    worker that turns the code-graded record of rejections into lessons. Same rule on both desks."""
    import datetime as _dt
    import opportunities as OPP
    sp = HOME / "state" / ("calibration_state.json" if desk == "training" else "live_calibration_state.json")
    try:
        st = json.loads(sp.read_text())
    except (OSError, ValueError):
        st = {"days": 0}
    for d in days:
        try:
            if _dt.date.fromisoformat(d).weekday() < 5:
                st["days"] = st.get("days", 0) + 1
        except (TypeError, ValueError):
            continue
    out = []
    if st.get("days", 0) >= OPP.CALIBRATE_EVERY_DAYS:
        text = OPP.calibration_text(desk, HOME, upto)
        if text:
            out.append({"type": "calibration_review" if desk == "training" else "live_calibration_review",
                        "days": st["days"], "text": text})
            st["days"] = 0
    sp.write_text(json.dumps(st))
    return out


def _view(build, ev, *args) -> dict:
    """A view that fails to build is reported in the prompt, never a lost event: the worker still has its
    own bars and lessons tools (the prompt says which)."""
    try:
        return build(ev, *args)
    except Exception as exc:
        return {"view_error": f"{type(exc).__name__}: {exc}"}


def live_tick(m=None, now=None, invoker=None) -> dict:
    """One LIVE tick: verify the guard, read what happened at the broker (tools/live_loop.py), turn
    each routed event into a demand of a LIVE kind -- unless the engine is halted, every account is
    waiting out a usage limit, or today's runaway guard (LIVE_DAILY_MAX_CALLS) is reached -- then drain.
    Spend is reported, never a reason to skip (operator, 2026-10-11). Every skip is recorded with its
    reason, never silent."""
    verify_guard()
    md = mode()
    if md != "LIVE":
        return {"ok": False, "detail": f"mode is {md}, not LIVE: the live loop does not run"}
    import live_loop
    res = live_loop.tick(m=m, now=now, home=HOME)
    eng = make_live_engine(invoker)
    eng.child_view = lambda kind, prompt, parent=None: live_child_view(kind, prompt, parent, m)
    expired = expire_stale_live_demands(eng)
    snap = res.get("snapshot")
    calls_cap = policy.LIVE_DAILY_MAX_CALLS
    calls = live_calls_today(eng)
    halt = eng.halt_reason()
    waiting = eng.store.waiting_for_limit()
    routed, skipped = [], []
    res["events"] = list(res["events"]) + calibration_due(
        "live", [e.get("day") for e in res["events"] if e["type"] == "live_day_closed"])
    for ev in res["events"]:
        typ = ev["type"]
        payload = {k: v for k, v in ev.items() if k != "type"}
        payload["snapshot"] = snap
        payload.update(_view(live_views, ev, snap, m))
        try:
            rt = eng.router(typ, payload)
        except ValueError as exc:
            skipped.append({"event": typ, "reason": str(exc)})
            continue
        if rt is None:
            routed.append({"event": typ, "demand": None})  # recorded, no model call by design
            continue
        why_not = (f"engine halted: {halt}" if halt else
                   "waiting out a usage limit" if waiting else
                   f"runaway guard: {calls} calls today (cap {calls_cap})" if calls >= calls_cap else None)
        if why_not:
            skipped.append({"event": typ, "reason": why_not})
            continue
        try:
            did = eng.enqueue(rt[0], rt[1], source=f"event:{typ}")
        except ValueError as exc:  # e.g. a prompt over max_prompt_chars: record it, keep the loop alive
            skipped.append({"event": typ, "reason": str(exc)})
            continue
        routed.append({"event": typ, "demand": did, "kind": rt[0]})
    if any(s["reason"].startswith("runaway guard") for s in skipped):
        runaway_alert(calls, calls_cap)
    if halt or waiting:
        what, detail = ("halted", halt) if halt else ("limited", waiting)
    else:
        what, detail = eng.drain()
    out = {"at": (snap or {}).get("utc"), "error": res.get("error"), "events": routed, "skipped": skipped,
           "expired": expired, "drain": what, "detail": detail, "spent_today": round(live_spent_today(eng), 4),
           "calls_today": live_calls_today(eng), "account": os.environ.get("CLAUDE_CONFIG_DIR", "default"),
           "equity": (snap or {}).get("equity")}
    if res.get("error") or routed or skipped or expired or what not in ("idle",):
        with open(HOME / "state" / "live_turns.jsonl", "a") as f:
            f.write(json.dumps(out, default=str) + "\n")
    return out


def _live_sleep_s(now=None) -> float:
    """Until just after the next M5 close (bars, scans and reconciliation all move on M5), or a
    coarse 30 min across the weekend gap (Friday 21:00 UTC to the Sunday 21:00 UTC reopen) when
    nothing can fill and the watchdog has already flattened."""
    from datetime import datetime, timedelta, timezone
    now = now or datetime.now(timezone.utc)
    wd, h = now.weekday(), now.hour
    if (wd == 4 and h >= 21) or wd == 5 or (wd == 6 and h < 21):
        reopen = (now + timedelta(days=(6 - wd) % 7)).replace(hour=21, minute=0, second=0, microsecond=0)
        return max(60.0, min(LIVE_WEEKEND_SLEEP_S, (reopen - now).total_seconds()))
    nxt = (now.replace(second=0, microsecond=0) + timedelta(minutes=5 - now.minute % 5))
    return max(30.0, (nxt - now).total_seconds() + 20)


def live_run(max_ticks: int | None = None) -> int:
    """The LIVE loop. One process at a time (state/live_loop.lock). Exits cleanly (code 0) the
    moment MODE is no longer LIVE, so a dashboard switch to TRAINING or OFF also ends it even if
    the service stop were missed. Pause (state/STOP) idles it without exiting."""
    import fcntl
    lock = open(HOME / "state" / "live_loop.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another live loop already holds state/live_loop.lock; not starting a second", flush=True)
        return 0
    import live_loop
    print(f"live loop up (mode {mode()}; {live_loop.window_text()}; runaway guard "
          f"{policy.LIVE_DAILY_MAX_CALLS} calls/day)", flush=True)
    n = 0
    while True:
        md = mode()
        if md != "LIVE":
            print(f"mode is {md}, not LIVE: live loop exits", flush=True)
            return 0
        if (HOME / "state" / "STOP").exists():
            time.sleep(60)
            continue
        try:
            res = live_tick()
        except SystemExit as exc:  # the guard check refused: stop, do not trade on a changed guard
            print(str(exc), flush=True)
            return 1
        except Exception as exc:
            res = {"error": f"tick crashed: {type(exc).__name__}: {exc}"}
        if res.get("error") or res.get("events") or res.get("skipped") or res.get("drain") not in (None, "idle"):
            print(json.dumps({k: res.get(k) for k in ("at", "error", "events", "skipped", "drain", "spent_today",
                                                       "calls_today", "account", "equity")}, default=str), flush=True)
        n += 1
        if max_ticks and n >= max_ticks:
            return 0
        time.sleep(_live_sleep_s())


def live_status() -> dict:
    eng = make_live_engine()
    n, cost = eng.store.totals()
    loop = HOME / "state" / "live_loop.json"
    store = _account_store()
    return {"mode": mode(), "engine_halt": eng.halt_reason() or "none", "invocations": n,
            "cost_usd_all_time": round(cost, 4), "spent_today_usd": round(live_spent_today(eng), 4),
            "calls_today": live_calls_today(eng), "runaway_guard_calls_per_day": policy.LIVE_DAILY_MAX_CALLS,
            "accounts": (store.all() if store else "single account (castl: fewer than 2 registered)"),
            "counts": eng.store.counts(),
            "loop_state": json.loads(loop.read_text()) if loop.exists() else None}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="driver")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("guard-write")
    sub.add_parser("research")
    sub.add_parser("field")
    t = sub.add_parser("turn")
    t.add_argument("--bars", type=int, default=288)
    r = sub.add_parser("run")
    r.add_argument("--turns", type=int, required=True)
    r.add_argument("--bars", type=int, default=288)
    sub.add_parser("status")
    sub.add_parser("live-tick")
    lr = sub.add_parser("live-run")
    lr.add_argument("--max-ticks", type=int)
    sub.add_parser("live-status")
    a = p.parse_args(argv)

    if a.cmd == "guard-write":
        print(json.dumps(guard_write(), indent=1))
    elif a.cmd == "research":
        print(json.dumps(research(), indent=1, default=str))
    elif a.cmd == "field":
        print(field())
    elif a.cmd == "turn":
        print(json.dumps(turn(a.bars), indent=1, default=str))
    elif a.cmd == "run":
        done_turns = 0
        while done_turns < a.turns:
            res = turn(a.bars)
            if res["drain"] == "limited":
                # Sleeping in-process would hold every symbol's candles (several GB, one now 1.9M bars) in
                # memory for the whole wait, which can be hours or, for a weekly cap, days. Instead: write
                # when to come back and exit cleanly (code 0 -- systemd's Restart=on-failure does not fire
                # on a clean exit). The ftmo-field-resume timer brings it back at the right time.
                resume_at = res["detail"] or (time.time() + 1800)
                (HOME / "state" / "paused_until.json").write_text(json.dumps(
                    {"resume_at": resume_at, "reason": "usage limit", "paused_at": time.time()}))
                when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(resume_at))
                print(f"pausing (usage limit) until {when}; exiting cleanly, nothing held in memory "
                     "while waiting; the resume timer will restart this service", flush=True)
                break
            if res["drain"] == "halted":
                print("stopping:", res["detail"], flush=True)
                break
            done_turns += 1
            print(json.dumps({"turn": done_turns, "at": res.get("at"), "stop": res.get("stop"), "bar": res["bar"],
                              "equity": res["equity"], "events": [e["event"] for e in res["events"]],
                              "drain": res["drain"],
                              "usage_pct_since_order": (res.get("castigation") or {}).get("pct"),
                              "castigation": (res.get("castigation") or {}).get("state")}), flush=True)
    elif a.cmd == "status":
        print(json.dumps(status(), indent=1, default=str))
    elif a.cmd == "live-tick":
        print(json.dumps(live_tick(), indent=1, default=str))
    elif a.cmd == "live-run":
        return live_run(a.max_ticks)
    elif a.cmd == "live-status":
        print(json.dumps(live_status(), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
