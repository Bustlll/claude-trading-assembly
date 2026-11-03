"""CASTIGATION: what the training desk has cost since its last order, and what happens when it burns usage without
trading (operator, 2026-11-01).

Operator: "if it eats more than 25 percent of usage and it didn't place a trade, it needs to be castigated ... this
ain't eating all my usage for free", and "it needs to be aware of its own context". The night before, the replay made
325 Claude calls ($20.54 API-equivalent: one 5-hour usage window and half of another) and placed no order in 5.5
replay days.

THE METER: what the training engine spent (state/selfcall.db cost_usd, API-equivalent) since the desk's last order,
as a share of one 5-hour usage window (USD_PER_WINDOW, measured 2026-11-01: 399 scripted calls, $8.48, moved account_2's real
5-hour meter from 9% to 89%, so $10.6 per window; the first guess, $13.7, assumed the night's chat use was nothing). An order_placed row in
the replay ledger resets it. Every training prompt carries it (self_text), so the desk knows what it has cost.

  < CASTIGATE_PCT (25%)   normal.
  >= 25%                  CASTIGATED: scouts are muted (they were 72% of the night's calls), every decision turn,
                          alert wake and swing turn must end with an order (paper.py tags it castigated), and the
                          castigation is written into the desk's record (a ledger row and a board lesson).
  >= STOP_PCT (50%)       STOPPED: training halts itself through its OWN marker (state/TRAINING_CASTIGATION_STOP,
                          read only by driver.py turn) and says so on the deltaForce dashboard's Errors tab. A Resume
                          by the operator clears it and gives one more CASTIGATE_PCT, still castigated.

TRAINING ONLY (operator, 2026-11-01: "castigation only runs on training and not on live trading -- live doesn't get
usage or nothing, it needs to trade perfectly, no rushing"). Nothing on the live path imports this module; the stop
never touches state/STOP or MANUAL_PAUSE, which the live loop and the mode switch share.

state/castigation.json: {"since_ts", "offset_usd", "marker", "state", "strikes", "castigated_at", "sim_since"}.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
STOP_MARKER = "TRAINING_CASTIGATION_STOP"
USD_PER_WINDOW = float(os.environ.get("FTMO_BLANK_USD_PER_5H_WINDOW", "10.6"))
CASTIGATE_PCT = float(os.environ.get("FTMO_BLANK_CASTIGATE_PCT", "25"))
STOP_PCT = float(os.environ.get("FTMO_BLANK_CASTIGATION_STOP_PCT", "50"))
DELTA_ERRORS_FILE = Path(os.environ.get(
    "FTMO_BLANK_DELTA_ERRORS_FILE", str(Path.home()) + "/deltaForceFTMO/deltaforce/data/runtime_state/errors.jsonl"))


def _path(home: Path) -> Path:
    return Path(home) / "state" / "castigation.json"


def load(home: Path = HOME) -> dict:
    try:
        return json.loads(_path(home).read_text())
    except (OSError, ValueError):
        return {}


def _save(home: Path, st: dict) -> None:
    p = _path(home)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    os.replace(tmp, p)


def last_order(home: Path = HOME) -> str | None:
    """A marker of the replay ledger's newest order_placed row ("bar:id"), or None if it has none."""
    p = Path(home) / "state" / "ledger.jsonl"
    if not p.exists():
        return None
    with open(p, "rb") as f:
        lines = f.read().splitlines()
    for line in reversed(lines):
        if b'"order_placed"' not in line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("type") == "order_placed":
            return f"{r.get('bar')}:{r.get('id')}"
    return None


def spent_since(home: Path, since_ts: float) -> tuple[float, int, dict]:
    """(API-equivalent USD, calls, calls per kind) of the training engine since since_ts."""
    db = Path(home) / "state" / "selfcall.db"
    if not db.exists():
        return 0.0, 0, {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT kind, COUNT(*), COALESCE(SUM(cost_usd), 0) FROM demands WHERE created >= ? "
                           "GROUP BY kind", (since_ts,)).fetchall()
    finally:
        con.close()
    return round(sum(r[2] for r in rows), 4), sum(r[1] for r in rows), {r[0]: r[1] for r in rows}


def meter(home: Path = HOME, now: float | None = None, sim_utc: str | None = None) -> dict:
    """Read (and keep) the meter. A new order since the last look resets it; a first look starts it now."""
    now = now or time.time()
    st = load(home)
    marker = last_order(home)
    if not st or st.get("marker") != marker:
        if st and st.get("state") in ("castigated", "stopped"):
            _note(home, {"event": "lifted", "utc": _utc(now), "by_order": marker})
        st = {"since_ts": now, "offset_usd": 0.0, "marker": marker, "state": "ok", "strikes": 0,
              "castigated_at": None, "sim_since": sim_utc}
        _save(home, st)
    if st.get("state") == "stopped" and not stopped(home):
        # the operator resumed it: one more CASTIGATE_PCT, still castigated, before it stops again
        st.update({"state": "castigated", "since_ts": now, "offset_usd": round(USD_PER_WINDOW * (STOP_PCT - CASTIGATE_PCT)
                                                                               / 100, 4)})
        _save(home, st)
        _note(home, {"event": "resumed_castigated", "utc": _utc(now)})
    usd, calls, kinds = spent_since(home, st["since_ts"])
    usd += float(st.get("offset_usd") or 0.0)
    return {**st, "usd": round(usd, 2), "calls": calls, "kinds": kinds,
            "pct": round(100 * usd / USD_PER_WINDOW, 1) if USD_PER_WINDOW > 0 else 0.0}


def peek(home: Path = HOME) -> str:
    """One read-only line for the dashboard: what training has spent since its last order, and its state."""
    st = load(home)
    if not st:
        return "usage since the last order: not measured yet (starts with the next training turn)."
    usd, calls, _ = spent_since(home, st["since_ts"])
    usd += float(st.get("offset_usd") or 0.0)
    pct = 100 * usd / USD_PER_WINDOW if USD_PER_WINDOW > 0 else 0.0
    return (f"usage since the last order: {pct:.0f}% of a 5-hour window ({calls} calls, ${usd:.2f}); "
            f"{st.get('state', 'ok').upper()} (castigated at {CASTIGATE_PCT:.0f}%, stopped at {STOP_PCT:.0f}%).")


def settle(home: Path = HOME, now: float | None = None, sim_utc: str | None = None, on_castigate=None) -> dict:
    """After a turn's calls: move the state on. on_castigate(m) runs once when the desk becomes castigated (the
    driver writes the record there). Returns the meter, with "changed" naming a transition if one happened."""
    m = meter(home, now, sim_utc)
    st = load(home)
    changed = None
    if m["state"] == "ok" and m["pct"] >= CASTIGATE_PCT:
        st.update({"state": "castigated", "strikes": int(st.get("strikes") or 0) + 1, "castigated_at": _utc(now)})
        changed = "castigated"
    elif m["state"] == "castigated" and m["pct"] >= STOP_PCT:
        st["state"] = "stopped"
        changed = "stopped"
    if changed:
        _save(home, st)
        m.update({k: st[k] for k in ("state", "strikes", "castigated_at")})
        _note(home, {"event": changed, "utc": _utc(now), "pct": m["pct"], "usd": m["usd"], "calls": m["calls"],
                     "sim_since": m.get("sim_since"), "sim_now": sim_utc})
        if changed == "castigated" and on_castigate:
            on_castigate(m)
        if changed == "stopped":
            _stop(home, m)
    m["changed"] = changed
    return m


def _stop(home: Path, m: dict) -> None:
    msg = (f"CASTIGATION STOP: the training replay spent {m['pct']:.0f}% of a 5-hour usage window ({m['calls']} calls, "
           f"${m['usd']:.2f} API-equivalent) since its last order and still placed none -- training paused itself. "
           "Resume from the dashboard gives it one more 25%, still castigated.")
    (Path(home) / "state" / STOP_MARKER).write_text(msg + "\n")
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), "ts_epoch": time.time(),
           "component": "ftmo_blank.training", "level": "error", "message": msg,
           "extra": {k: m.get(k) for k in ("pct", "usd", "calls", "sim_since")}}
    try:
        if DELTA_ERRORS_FILE.parent.is_dir():
            with open(DELTA_ERRORS_FILE, "a") as f:
                f.write(json.dumps(rec) + "\n")
    except OSError:
        pass
    print("!!! " + msg, flush=True)


def stopped(home: Path = HOME) -> str | None:
    """The training-only stop's reason while it stands (driver.py turn halts on it), else None."""
    p = Path(home) / "state" / STOP_MARKER
    try:
        return p.read_text().strip() or "castigation stop"
    except OSError:
        return None


def _note(home: Path, rec: dict) -> None:
    with open(Path(home) / "state" / "castigation_log.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")


def _utc(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts or time.time()))


def self_text(m: dict, scout: bool = False) -> str:
    """YOUR OWN CONTEXT: what this desk is and what it has cost. Facts, no push: with "a turn that skips teaches nothing" in
    every prompt the scouts handed on 27 of 29 movers and the desk placed 10 trades in a day, 7 of them losers (2026-11-01)."""
    cost = (f"YOUR OWN CONTEXT (code-measured): you are the TRAINING desk, a paper replay of real history. Since the last order: "
            f"{m.get('calls', 0)} calls, {m.get('pct', 0):.0f}% of one 5-hour usage window.")
    if scout:
        return cost + " Calls cost the operator's usage: decide from what is attached."
    return (cost + f" At {CASTIGATE_PCT:.0f}% without an order the desk is castigated (scouts muted, every turn must trade); at "
            f"{STOP_PCT:.0f}% training stops. Everything this turn needs is attached below: call a tool only for what is missing.")


def castigation_text(m: dict) -> str:
    """The directive at the top of a decision turn, an alert wake or a swing turn while castigated."""
    return (f"CASTIGATED. Since your last order this desk has burned {m.get('pct', 0):.0f}% of a 5-hour usage window "
            f"({m.get('calls', 0)} calls, ${m.get('usd', 0):.2f}) and placed nothing. Scouts are muted until you trade. "
            "THIS TURN MUST END WITH AN ORDER: the best setup in front of you -- a mover, a waiting alert's level, a "
            "swing -- with the stop at real structure, the normal per-trade risk, and --why starting with \"CASTIGATED\" "
            "and the setup code. A skip is not an answer this turn. Only if the guard refuses every candidate (market "
            f"shut, a halt) may you end without one: then name what refused each. At {STOP_PCT:.0f}% training stops.")


def record_lesson(m: dict, post) -> None:
    """The castigation into the desk's own record, so the next workers read it: post(worker, topic, text)."""
    post("review", "lesson",
         f"CASTIGATED (entry): {m.get('calls', 0)} calls and ${m.get('usd', 0):.2f} ({m.get('pct', 0):.0f}% of a "
         "usage window) since the last order"
         + (f" (replay {m['sim_since']})" if m.get("sim_since") else "")
         + " without one entry. Every skip had a reason (A1, A5 on almost every mover); together they produced "
           "nothing to learn from. When a setup code fits at structure with a first target >= 1.5R, take it at the "
           "normal risk instead of waiting for a perfect one; a plan at its level is executed, not re-litigated.")
