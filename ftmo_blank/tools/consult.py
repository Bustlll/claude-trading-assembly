"""CONSULT: a trader asks the chart or timing specialist NOW and gets the answer inside its own turn (2026-10-19).

Operator, 2026-10-19: "if some trader worker wants to know in precision the trade that it's about to take, as for
analysis and timing, it should be near immediate that it is able to contact the specialists and they give it a clear
answer". Before this the only way to ask was next_demands: the specialist ran AFTER the asking turn had ended, could
hand nothing back (may_spawn []), and the live decider was told to "decide at the next turn once they have posted"
-- an hour later. So nobody asked: chart was called once in all of training, timing 29 times, live 0 and 2.

Now the asking worker runs this tool like any other command and waits ~20-60 s for the answer:

  python3 tools/consult.py live|training chart  --symbol S --question "..." [--side buy|sell] [--entry P] [--sl P] [--tp P]
  python3 tools/consult.py live|training timing --symbol S --question "..."

The specialist (the existing chart/timing kinds, live_chart/live_timing on the live desk -- same model, effort,
budget and tools) is called synchronously through the desk's own engine and account failover, with its view built by
code (tools/desk_shared.py, the same on both desks): chart gets the symbol file (M5/M15/H1/D1 candles, today's thread,
the lessons, the desk's record) plus the SWEEP DEPTH table; timing gets the clock and the HOUR TABLE. Its answer is
stored as a child of the asking turn (selfcall passes SELFCALL_DEMAND_ID), posted to the desk's board (topic consult,
which marks a following order consulted_analysis), logged to state/{live_,train_}consults.jsonl and printed back.
The desk in the command is pinned by each kind's permission rule, so a live worker can only consult live data.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME))
sys.path.insert(0, str(HOME / "tools"))
sys.path.insert(0, str(HOME.parent / "selfcall"))

TIMEOUT_S = int(os.environ.get("FTMO_BLANK_CONSULT_TIMEOUT_S", "100"))   # the asker's Bash call waits up to 2 min
MAX_PER_TURN = 3
KIND = {("live", "chart"): "live_chart", ("live", "timing"): "live_timing", ("live", "vault"): "live_vault",
        ("training", "chart"): "chart", ("training", "timing"): "timing", ("training", "vault"): "vault"}


def _plan_text(a) -> str:
    bits = [f"side {a.side}" if a.side else "", f"entry {a.entry}" if a.entry is not None else "",
            f"stop {a.sl}" if a.sl is not None else "", f"target {a.tp}" if a.tp is not None else ""]
    bits = [b for b in bits if b]
    return ("THE TRADE IT IS ABOUT: " + ", ".join(bits)) if bits else "THE TRADE IT IS ABOUT: (no levels given)"


def _vault_view(desk: str, symbol: str, side: str | None, entry, sl, tp) -> str:
    """The vault's evidence for one entry being decided now (tools/vault.py): look-alikes and the tables."""
    import vault as V
    if not side:
        return "VAULT CHECK: no --side given; the look-alikes need the trade's direction. The tables:\n" + V.report(
            V.load(desk, _sim_now(desk)), desk, symbol)
    f = V.features_now(desk, symbol, side, entry, sl, tp)
    rows = V.load(desk, _sim_now(desk))
    if not f:
        return "VAULT CHECK: no bars to read this entry. The tables:\n" + V.report(rows, desk, symbol)
    return V.similar(f, symbol, side, rows, desk=desk) + "\n\n" + V.report(rows, desk, symbol)


def _sim_now(desk: str):
    if desk != "training":
        return None
    sys.path.insert(0, str(HOME / "agent_code"))
    import kstore as K
    t = K.now_utc(HOME)
    return t.timestamp() if t else None


def _desk_view(desk: str, role: str, symbol: str, question: str, plan: str, levels: dict | None = None
               ) -> tuple[str, object]:
    """(the specialist's view, the engine to run it on) -- both desks through tools/desk_shared.py."""
    import driver as D
    import desk_shared as DS
    import manage_core as MC
    if role == "vault":
        lv = levels or {}
        view = _vault_view(desk, symbol, lv.get("side"), lv.get("entry"), lv.get("sl"), lv.get("tp"))
        return view, (D.make_live_engine() if desk == "live" else D.make_engine())
    if desk == "live":
        import live_broker as LB
        import live_loop as LL
        import live_market as LM
        m = LB._mt5_client()
        now = LB._now_utc()
        if role == "chart":
            view = (D.live_symbol_file(symbol, question + "\n" + plan, m) + "\n\n"
                    + DS.sweep_depth(LM.bars(symbol, 96, "M15", m=m)))
        else:
            view = (DS.clock_text(now, LL.window_text(), LL.in_window(now), LL.next_decision_utc(now)) + "\n\n"
                    + DS.hour_table(LM.m5_history(symbol, 5 * 288, m=m)) + "\n\n"
                    + DS.bars_text(symbol, "H1", LM.bars(symbol, 24, "H1", m=m)))
        return view, D.make_live_engine()
    import paper as P
    paper = P.Paper()
    t = paper.t_now()
    if t is None:
        raise RuntimeError("the replay has no bars yet")
    now = P.bar_utc(t) + dt.timedelta(minutes=5)
    if role == "chart":
        view = (D.train_symbol_file(symbol, question + "\n" + plan, paper) + "\n\n"
                + DS.sweep_depth(paper.recent_bars(symbol, 96, "M15")))
    else:
        view = (DS.clock_text(now, P.train_window_text(), P.in_train_window(t), P.train_next_decision_utc(t)) + "\n\n"
                + DS.hour_table(paper.recent_bars(symbol, 5 * 288, "M5")) + "\n\n"
                + DS.bars_text(symbol, "H1", paper.recent_bars(symbol, 24, "H1")))
    _ = MC   # the clock text imports it; kept explicit for readers
    return view, D.make_engine()


def consult(desk: str, role: str, symbol: str, question: str, plan: str, parent: str | None = None,
            view_builder=None, home: Path = HOME, levels: dict | None = None) -> dict:
    import desk_shared as DS
    from selfcall.engine import LimitHit
    t0 = time.time()
    try:
        view, eng = (view_builder(desk, role, symbol, question, plan) if view_builder else
                     _desk_view(desk, role, symbol, question, plan, levels))
    except Exception as exc:
        return {"ok": False, "reason": f"the specialist's view could not be built ({type(exc).__name__}: {exc}) -- "
                                       "decide without it, or read the bars yourself"}
    kind = KIND[(desk, role)]
    depth, parent_kind = 1, None
    if parent:
        row = eng.store.get(parent)
        if row is not None:
            depth, parent_kind = int(row["depth"] or 0) + 1, row["kind"]
        n = eng.store.db.execute("SELECT COUNT(*) FROM demands WHERE parent=? AND source='consult'", (parent,)).fetchone()[0]
        if n >= MAX_PER_TURN:
            return {"ok": False, "reason": f"already {n} consults this turn (at most {MAX_PER_TURN}): decide with what you have"}
    task = {"chart": DS.CHART_CONSULT_TASK, "timing": DS.TIMING_CONSULT_TASK, "vault": DS.VAULT_CONSULT_TASK}[role]
    prompt = (f"CONSULT ({role.upper()}) on {symbol}, asked by a {parent_kind or 'trader'} worker deciding RIGHT NOW.\n"
              f"THE QUESTION: {question.strip()}\n{plan}\n\n{view}\n\n{task}")
    did = eng.store.add(kind, prompt, "", parent, depth, "consult", dedupe=False)
    eng.store.set(did, status="running", started=time.time())
    eng.limits = {**eng.limits, "timeout_s": TIMEOUT_S}
    try:
        res = eng.invoker(dict(eng.store.get(did)))
    except LimitHit as exc:
        eng.store.set(did, status="failed", error=f"usage limit: {str(exc)[:300]}", finished=time.time())
        return {"ok": False, "demand": did, "reason": "the specialist is unavailable (every account is waiting out a "
                                                      "usage limit) -- decide without it"}
    except Exception as exc:
        eng.store.set(did, status="failed", error=str(exc)[:2000], finished=time.time())
        eng._write_record(did)
        return {"ok": False, "demand": did, "reason": f"the specialist gave no answer in time ({str(exc)[:160]}) -- "
                                                      "decide without it"}
    ans = res["answer"]
    eng.store.set(did, status=ans["status"], answer=json.dumps(ans), cost_usd=res["cost_usd"],
                  session_id=res["session_id"], finished=time.time(),
                  links=json.dumps({"spawned": [], "rejected": [], "denials": res.get("denials", [])}))
    eng._write_record(did)
    summary = str(ans.get("summary") or "").strip()
    findings = [str(f) for f in (ans.get("findings") or [])][:5]
    secs = round(time.time() - t0, 1)
    post = (summary.splitlines()[0] if summary else f"{role} consult on {symbol}: no summary")
    post_text = (post + (" || " + " | ".join(findings[:3]) if findings else ""))[:1500]
    try:
        if desk == "live":
            import live_board as BD
        else:
            import board as BD
        BD.post(role, "consult", post_text, symbol, home=home)
    except BaseException as exc:   # board.post raises SystemExit on a bad worker name; never lose the answer for it
        post_text += f" (board post failed: {exc})"
    rec = {"utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "desk": desk, "role": role,
           "symbol": symbol, "parent": parent, "parent_kind": parent_kind, "demand": did, "verdict": post[:300],
           "cost_usd": round(res["cost_usd"], 4), "seconds": secs, "question": question[:300]}
    with open(Path(home) / "state" / ("live_consults.jsonl" if desk == "live" else "train_consults.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")
    return {"ok": True, "demand": did, "summary": summary, "findings": findings, "cost_usd": round(res["cost_usd"], 4),
            "seconds": secs}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="consult.py", description="ask the chart or timing specialist now")
    p.add_argument("desk", choices=["live", "training"])
    p.add_argument("role", choices=["chart", "timing", "vault"])
    p.add_argument("--symbol", required=True)
    p.add_argument("--question", required=True)
    p.add_argument("--side", choices=["buy", "sell"])
    p.add_argument("--entry", type=float)
    p.add_argument("--sl", type=float)
    p.add_argument("--tp", type=float)
    a = p.parse_args(argv)
    if len(a.question.strip()) < 10:
        print("REFUSED: ask a real question (>= 10 characters): what level, what time, what you need to decide")
        return 0
    from guard import rules as R
    sym = a.symbol
    if sym not in R.INSTRUMENTS:   # tolerate case: US100.CASH -> US100.cash
        sym = next((s for s in R.INSTRUMENTS if s.upper() == sym.upper()), sym)
    res = consult(a.desk, a.role, sym, a.question, _plan_text(a), os.environ.get("SELFCALL_DEMAND_ID") or None,
                  levels={"side": a.side, "entry": a.entry, "sl": a.sl, "tp": a.tp})
    if not res["ok"]:
        print(f"NO ANSWER: {res['reason']}")
        return 0
    print(res["summary"])
    for f in res["findings"]:
        print(f"- {f}")
    print(f"({a.role} consult {res['demand']}, {res['seconds']}s, ${res['cost_usd']}; posted to the board, topic consult)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
