"""SCOUT EVAL: can a cheaper scout screen moves as well as the Sonnet scout -- Haiku alone, or Haiku with the
desk's own setup library (evals/scout/setup_library.md)?

Same market moments for every arm: real in-window hourly scans (the exact paper.py scan, the same once-per-
symbol-per-day rule), the candles a production scout now gets in its prompt (12 M15 + 24 M5 per candidate,
top 3), no tools. What happened next is measured on the M5 bars that followed, which no arm ever sees:
  * each arm's OWN plan (trade_now / alert, its side, stop and target) is simulated -> R
  * an oracle per candidate (either side, entry next open, stop 2 x ATR(M15), target 1.5x the stop, 24h):
    was there a clean trade at all, and on which side -> recall of good setups, passes on noise
Never touches state/: everything lives in evals/scout/<run>/.

  python3 tools/scout_eval.py build --run pilot --n 30 --from 2025-07-01 --to 2025-09-30
  python3 tools/scout_eval.py run --run pilot --arms sonnet,haiku,haiku_lib [--workers 4]
  python3 tools/scout_eval.py report --run pilot
Calls go to whichever Claude login CLAUDE_CONFIG_DIR points at (unset = ~/.claude).
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "tools"))
sys.path.insert(0, str(HOME))
import paper as P  # noqa: E402
from guard import rules as R  # noqa: E402

OUT = HOME / "evals" / "scout"
LIBRARY = OUT / "setup_library.md"
HORIZON_S = 24 * 3600
ALERT_WAIT_S = 8 * 3600
MAX_CANDS = 3

ARMS = {  # name -> (model, effort, with_library)
    "sonnet": ("sonnet", "medium", False),       # the production scout today
    "haiku": ("haiku", "medium", False),
    "haiku_lib": ("haiku", "medium", True),
    "sonnet_lib": ("sonnet", "medium", True),
    # smoke test 2026-10-18: Haiku at medium effort thought for up to 16k output tokens ($0.11, 143 s) on one
    # case -- dearer than the Sonnet scout (~1k output tokens). Low effort is the only cheap Haiku.
    "haiku_low": ("haiku", "low", False),
    "haiku_lib_low": ("haiku", "low", True),
}

SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["dismiss", "trade_now", "alert"]},
                    "setup": {"type": "string"},
                    "side": {"type": ["string", "null"], "enum": ["buy", "sell", None]},
                    "entry": {"type": ["number", "null"]},
                    "stop": {"type": ["number", "null"]},
                    "target": {"type": ["number", "null"]},
                    "reason": {"type": "string"},
                },
                "required": ["symbol", "verdict", "setup", "side", "entry", "stop", "target", "reason"],
            },
        }
    },
    "required": ["verdicts"],
}


def scratch_paper() -> "P.Paper":
    """A Paper over the real feeds with an empty, throwaway state dir: nothing is ever saved."""
    tmp = Path(tempfile.mkdtemp(prefix="scout_eval_"))
    (tmp / "state").mkdir()
    (tmp / "feed").symlink_to(HOME / "feed")
    return P.Paper(home=tmp)


def _index_at(p, when: datetime) -> int:
    lo, hi = 0, len(p.timeline)
    while lo < hi:
        mid = (lo + hi) // 2
        if P.bar_utc(p.timeline[mid]) < when:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _future(p, sym: str, t: int) -> list[list]:
    ts = p._symbol_ts(sym)
    i = bisect.bisect_right(ts, t)
    out = []
    for b in p.feeds[sym][i:]:
        if b["t"] - t > HORIZON_S:
            break
        out.append([b["t"], b["o"], b["h"], b["l"], b["c"], 1 if P.in_train_window(b["t"]) else 0])
    return out


def _walk(fut: list[list], k: int, side: str, entry: float, stop: float, target: float | None) -> dict:
    """From bar k on: stop first when both levels sit in one bar (paper.py's rule). R in price terms."""
    risk = (entry - stop) if side == "buy" else (stop - entry)
    if risk <= 0:
        return {"result": "invalid", "r": 0.0}
    if target is None:
        target = entry + 2 * risk if side == "buy" else entry - 2 * risk
    for _, o, h, l, c, _w in fut[k:]:
        if side == "buy":
            if l <= stop:
                return {"result": "loss", "r": round(((min(o, stop)) - entry) / risk, 3)}
            if h >= target:
                return {"result": "win", "r": round((target - entry) / risk, 3)}
        else:
            if h >= stop:
                return {"result": "loss", "r": round((entry - max(o, stop)) / risk, 3)}
            if l <= target:
                return {"result": "win", "r": round((entry - target) / risk, 3)}
    last = fut[-1][4] if fut else entry
    return {"result": "open", "r": round(((last - entry) if side == "buy" else (entry - last)) / risk, 3)}


def oracle(fut: list[list], atr: float, half: float) -> dict:
    out = {}
    if not fut or atr <= 0:
        return {"buy": "none", "sell": "none", "good": None}
    for side in ("buy", "sell"):
        e = fut[0][1] + (half if side == "buy" else -half)
        sd = 2 * atr
        stop, tgt = (e - sd, e + 1.5 * sd) if side == "buy" else (e + sd, e - 1.5 * sd)
        out[side] = _walk(fut, 0, side, e, stop, tgt)["result"]
    wins = [s for s in ("buy", "sell") if out[s] == "win"]
    out["good"] = wins[0] if len(wins) == 1 else ("both" if len(wins) == 2 else None)
    return out


def build(run: str, n: int, frm: str, to: str, seed: int) -> Path:
    p = scratch_paper()
    i0 = _index_at(p, datetime.fromisoformat(frm).replace(tzinfo=timezone.utc))
    i1 = _index_at(p, datetime.fromisoformat(to).replace(tzinfo=timezone.utc))
    events, flagged = [], {}
    for i in range(i0, i1):
        t = p.timeline[i]
        if P.bar_close_local(t).minute != 0 or not P.in_train_window(t):
            continue
        p.s["cursor"] = i + 1
        day = P.bar_close_local(t).date().isoformat()
        seen = flagged.setdefault(day, set())
        cands = [c for c in p.scan_candidates() if c["symbol"] not in seen][:5]
        if not cands:
            continue
        seen.update(c["symbol"] for c in cands)
        events.append((i, cands))
    random.Random(seed).shuffle(events)
    cases = []
    for i, cands in sorted(events[:n]):
        p.s["cursor"] = i + 1
        t = p.timeline[i]
        cc = []
        for c in cands[:MAX_CANDS]:
            sym = c["symbol"]
            m15 = p.recent_bars(sym, 15, "M15")
            atr = sum(b["h"] - b["l"] for b in m15[-14:]) / max(1, len(m15[-14:]))
            half = R.spec(sym)["spread_price_snapshot"] / 2
            fut = _future(p, sym, t)
            m5 = p.recent_bars(sym, 24, "M5")
            cc.append({**c, "m15": P.bars_text(sym, "M15", m15[-12:]), "m5": P.bars_text(sym, "M5", m5),
                       "last": m5[-1]["c"] if m5 else None,
                       "atr_m15": atr, "half_spread": half, "future": fut, "oracle": oracle(fut, atr, half)})
        cases.append({"i": i, "utc": P.bar_utc(t).strftime("%Y-%m-%d %H:%M UTC"),
                      "madrid": P.bar_close_local(t).strftime("%a %H:%M"), "candidates": cc,
                      "all_candidates": cands})
    d = OUT / run
    d.mkdir(parents=True, exist_ok=True)
    (d / "cases.json").write_text(json.dumps(cases))
    print(f"{len(cases)} cases from {len(events)} in-window scan events {frm}..{to} -> {d / 'cases.json'}")
    return d


def prompt_for(case: dict, with_library: bool) -> str:
    names = ", ".join(f"{c['symbol']} ({c['pct_move']:+.2f}% over {c['bars']} bars)" for c in case["all_candidates"])
    bars = "\n\n".join(f"{c['m15']}\n\n{c['m5']}" for c in case["candidates"])
    lib = ""
    if with_library:
        lib = ("\n\n" + LIBRARY.read_text() + "\nIn \"setup\" name the card your plan matches (S1-S4), or the "
               "anti-pattern (A1-A5) that makes you dismiss, or \"other\".\n")
    return (
        "You are the SCOUT of a paper FTMO-challenge trading desk (10,000 USD account, +5% in 10 business days, "
        "never 5% daily / 10% total loss). You are the first step: a trader acts only on what you pass on. A real "
        "setup you dismiss is a lost trade; noise you pass on costs the trader a call and maybe a loss.\n\n"
        f"HOURLY SCAN at {case['utc']} ({case['madrid']} Europe/Madrid, inside the 07:00-20:00 trading window): "
        f"{names} moved past the 0.30% threshold IN THE LAST HOUR. The clock is stopped on this bar: you see the "
        "move as it happens. Below are the last 12 closed M15 bars and the last 24 closed M5 bars of the top "
        f"{len(case['candidates'])} (utc_open session open high low close).\n\n{bars}{lib}\n\n"
        "For EACH symbol whose bars are shown, give one verdict:\n"
        "  dismiss   -- noise, a gap, exhausted, one correlated move already counted, or no structure;\n"
        "  trade_now -- a clean entry right now (it fills at the next bar open): side, stop at real structure, target;\n"
        "  alert     -- a real move whose entry is a retest or break still to come: side, entry = the alert level "
        "(price must trade there within 8 hours, inside the window), stop, target.\n"
        "Prices must be real levels from these bars. Reason in one or two sentences. Answer only in the schema.")


def call(model: str, effort: str | None, prompt: str, cwd: Path) -> dict:
    argv = ["claude", "-p", "--output-format", "json", "--model", model, "--tools", "",
            "--permission-mode", "dontAsk", "--max-budget-usd", "0.40", "--json-schema", json.dumps(SCHEMA)]
    if effort:
        argv += ["--effort", effort]
    t0 = time.time()
    proc = subprocess.run(argv, input=prompt, capture_output=True, text=True, cwd=cwd, timeout=600)
    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"error": (proc.stdout + proc.stderr)[-400:], "secs": time.time() - t0}
    return {"answer": env.get("structured_output"), "cost": env.get("total_cost_usd"), "error": None
            if env.get("subtype") == "success" and not env.get("is_error") else str(env.get("result"))[:400],
            "usage": env.get("usage"), "secs": round(time.time() - t0, 1)}


def usage_ok(account: str | None, max_5h: float, max_week: float) -> tuple[bool, str]:
    """castl's meters for one login: stop before the account reaches the operator's ceiling."""
    if not account:
        return True, "no guard"
    import re
    out = subprocess.run(["castl", "usage"], capture_output=True, text=True, timeout=60).stdout
    for line in out.splitlines():
        if line.startswith(account) and "5h" in line:
            m5 = re.search(r"5h\s+([\d.]+)%", line)
            mw = re.search(r"week\s+([\d.]+)%", line)
            if m5 and mw:
                a, b = float(m5.group(1)), float(mw.group(1))
                return (a < max_5h and b < max_week), f"{account} 5h {a}% week {b}%"
    return False, f"no meter line for {account}"


def run_interleaved(run_name: str, arms: list[str], account: str | None, max_5h: float, max_week: float):
    """Every arm on case k before case k+1, with a usage check between cases: a run stopped by the ceiling
    still compares the arms on exactly the same moments."""
    d = OUT / run_name
    cases = json.loads((d / "cases.json").read_text())
    cwd = Path(tempfile.mkdtemp(prefix="scout_eval_cwd_"))
    paths = {a: d / f"answers_{a}.json" for a in arms}
    done = {a: (json.loads(paths[a].read_text()) if paths[a].exists() else {}) for a in arms}
    for k in range(len(cases)):
        todo = [a for a in arms if str(k) not in done[a] or done[a][str(k)].get("error")]
        if not todo:
            continue
        ok, why = usage_ok(account, max_5h, max_week)
        print(f"case {k}: {why}", flush=True)
        if not ok:
            print("stopping: usage ceiling reached", flush=True)
            return

        def one(arm):
            model, effort, lib = ARMS[arm]
            return arm, call(model, effort, prompt_for(cases[k], lib), cwd)

        with ThreadPoolExecutor(max_workers=len(todo)) as ex:
            for arm, res in ex.map(one, todo):
                done[arm][str(k)] = res
                paths[arm].write_text(json.dumps(done[arm]))
                err = res.get("error")
                print(f"  {arm}: {'ERROR ' + err[:120] if err else 'ok'} ${res.get('cost')} {res.get('secs')}s", flush=True)
                if err and any(m in err.lower() for m in ("limit", "rate")):
                    print("stopping: usage limit", flush=True)
                    return


def run(run_name: str, arms: list[str], workers: int):
    d = OUT / run_name
    cases = json.loads((d / "cases.json").read_text())
    cwd = Path(tempfile.mkdtemp(prefix="scout_eval_cwd_"))
    for arm in arms:
        model, effort, lib = ARMS[arm]
        path = d / f"answers_{arm}.json"
        done = json.loads(path.read_text()) if path.exists() else {}
        todo = [k for k in range(len(cases)) if str(k) not in done or done[str(k)].get("error")]

        def one(k):
            return k, call(model, effort, prompt_for(cases[k], lib), cwd)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for k, res in ex.map(one, todo):
                done[str(k)] = res
                path.write_text(json.dumps(done))
                err = res.get("error")
                print(f"{arm} case {k}: {'ERROR ' + err[:120] if err else 'ok'} ${res.get('cost')} {res.get('secs')}s",
                      flush=True)
                if err and any(m in err.lower() for m in ("limit", "rate")):
                    print("stopping: usage limit", flush=True)
                    return


def _sim_plan(v: dict, cand: dict) -> dict:
    fut, half = cand["future"], cand["half_spread"]
    side = v.get("side")
    if v["verdict"] == "dismiss" or side not in ("buy", "sell") or not fut or v.get("stop") is None:
        return {"result": "invalid" if v["verdict"] != "dismiss" else "none", "r": 0.0}
    k = 0
    if v["verdict"] == "alert":
        lvl = v.get("entry")
        if lvl is None:
            return {"result": "invalid", "r": 0.0}
        t0 = fut[0][0]
        k = None
        last = cand["last"] if cand.get("last") is not None else fut[0][1]
        for j, (t, o, h, l, c, w) in enumerate(fut):
            if t - t0 > ALERT_WAIT_S:
                break
            if w and ((lvl <= last and l <= lvl) or (lvl >= last and h >= lvl)):
                k = j + 1
                break
        if k is None or k >= len(fut):
            return {"result": "not_triggered", "r": 0.0}
    entry = fut[k][1] + (half if side == "buy" else -half)
    stop = float(v["stop"])
    if v["verdict"] == "alert" and ((side == "buy" and entry <= stop) or (side == "sell" and entry >= stop)):
        return {"result": "gapped", "r": 0.0}  # price went through the level and the stop: a trader would not enter
    return _walk(fut, k, side, entry, stop, None if v.get("target") is None else float(v["target"]))


def report(run_name: str) -> str:
    d = OUT / run_name
    cases = json.loads((d / "cases.json").read_text())
    arms = [a for a in ARMS if (d / f"answers_{a}.json").exists()]
    good = sum(1 for c in cases for x in c["candidates"] if x["oracle"]["good"])
    total = sum(len(c["candidates"]) for c in cases)
    lines = [f"# Scout eval `{run_name}`: {len(cases)} scan moments, {total} candidates, oracle-good {good}",
             "", "Oracle: either side, entry next M5 open, stop 2 x ATR(M15), target 1.5x the stop, 24h. "
             "Plans are simulated with each arm's own side, stop and target (stop first in a two-level bar).", "",
             "| arm | calls ok | cost $ | acted (now/alert) | alerts hit | plan R total | wins/losses/open | invalid "
             "| recall of good | acted on noise | agree w/ sonnet |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    verdicts = {}
    for arm in arms:
        ans = json.loads((d / f"answers_{arm}.json").read_text())
        ok = [k for k, a in ans.items() if not a.get("error") and a.get("answer")]
        cost = sum(a.get("cost") or 0 for a in ans.values())
        acted = now = alert = hit = inval = rec = noise = 0
        w = lo = op = 0
        rt = 0.0
        vmap = {}
        for k in ok:
            case = cases[int(k)]
            by = {v["symbol"]: v for v in ans[k]["answer"].get("verdicts", [])}
            for cand in case["candidates"]:
                v = by.get(cand["symbol"], {"verdict": "dismiss", "side": None})
                vmap[(k, cand["symbol"])] = (v["verdict"] != "dismiss", v.get("side"))
                g = cand["oracle"]["good"]
                if v["verdict"] == "dismiss":
                    continue
                acted += 1
                now += v["verdict"] == "trade_now"
                alert += v["verdict"] == "alert"
                res = _sim_plan(v, cand)
                if res["result"] == "invalid":
                    inval += 1
                elif res["result"] not in ("not_triggered", "gapped"):
                    hit += v["verdict"] == "alert"
                    rt += res["r"]
                    w += res["result"] == "win"
                    lo += res["result"] == "loss"
                    op += res["result"] == "open"
                if g and (g == "both" or g == v.get("side")):
                    rec += 1
                if not g:
                    noise += 1
        verdicts[arm] = vmap
        agree = ""
        if arm != "sonnet" and "sonnet" in verdicts:
            common = [x for x in vmap if x in verdicts["sonnet"]]
            same = sum(1 for x in common if vmap[x][0] == verdicts["sonnet"][x][0]
                       and (not vmap[x][0] or vmap[x][1] == verdicts["sonnet"][x][1]))
            agree = f"{same}/{len(common)}"
        lines.append(f"| {arm} | {len(ok)}/{len(cases)} | {cost:.2f} | {acted} ({now}/{alert}) | {hit} | {rt:+.2f} | "
                     f"{w}/{lo}/{op} | {inval} | {rec}/{good} | {noise} | {agree} |")
    lines += ["", "## Per candidate (verdict side setup -> simulated R; oracle good side)", ""]
    for k, case in enumerate(cases):
        for cand in case["candidates"]:
            row = [f"{case['utc']} {cand['symbol']} {cand['pct_move']:+.2f}% oracle={cand['oracle']['good']}"]
            for arm in arms:
                a = json.loads((d / f"answers_{arm}.json").read_text()).get(str(k), {})
                by = {v["symbol"]: v for v in ((a.get("answer") or {}).get("verdicts") or [])}
                v = by.get(cand["symbol"])
                if not v:
                    row.append(f"{arm}: -")
                    continue
                res = _sim_plan(v, cand)
                row.append(f"{arm}: {v['verdict']} {v.get('side') or ''} {v.get('setup', '')[:12]} "
                           f"{res['result']} {res['r']:+.2f}")
            lines.append("- " + " | ".join(row))
    text = "\n".join(lines)
    (d / "report.md").write_text(text)
    return text


# ==== MASKED ARM (2026-11-01, todos/010): the same no-tools, same-moments idea as above, with what that lacked --------------
# The worker model REMEMBERS 2025 (it recalled gold's month-ends and the 23-24 June oil fall from memory), so a dated moment does
# not test its read of the chart. Here the instrument, the dates and the price scale are hidden per moment; each trade is graded
# against its mirror (the same distances on the other side), which removes the market's drift; a swing packet; and --playbook,
# to test what a learn step wrote against the blank desk on the same held-out moments. First built as a separate tools/fastloop.py
# by mistake (this file already existed); merged here the same day.
#   scout_eval.py masked run --name NAME --style swing|intraday --n 40 --seed 1 [--dated] [--from D --to D] [--playbook FILE] [--max-usd 2]
#   scout_eval.py masked report NAME [NAME ...]
import concurrent.futures as cf  # noqa: E402
import datetime as dt  # noqa: E402
import re  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

MASK_OUT = OUT / "masked"
UNIVERSE = {"XAUUSD": "metal", "XAGUSD": "metal", "XPTUSD": "metal", "XCUUSD": "metal", "USOIL.cash": "energy", "US100.cash": "index",
            "US500.cash": "index", "GER40.cash": "index", "EURUSD": "fx", "GBPUSD": "fx", "USDJPY": "fx", "AUDUSD": "fx", "USDCAD": "fx"}
NY, MAD = ZoneInfo("America/New_York"), ZoneInfo("Europe/Madrid")
PACKS = {"swing": (("D1", 12), ("H4", 24), ("H1", 12)), "intraday": (("H1", 24), ("M15", 24), ("M5", 24))}
HORIZON = {"swing": 288 * 5, "intraday": 288}
ASK = {"swing": "You decide once a day, about an hour before the daily rollover. A position is held overnight, up to five days, on its stop and target.",
       "intraday": "You decide right now, inside the trading day. A position is held for hours, up to a day, on its stop and target."}
_feeds: dict = {}


def m_feed(sym):
    if sym not in _feeds:
        _feeds[sym] = P.load_feed(HOME / "feed" / f"{sym}_M5.csv")
    return _feeds[sym]


def m_moments(style, n, seed, d0, d1):
    rnd = random.Random(f"{style}-{seed}")
    out, syms = [], list(UNIVERSE)
    while len(out) < n:
        sym = rnd.choice(syms)
        b = m_feed(sym)
        i = rnd.randrange(288 * 30, len(b) - 288 * 6)
        u = P.bar_utc(b[i]["t"])
        if not d0 <= u.strftime("%Y-%m-%d") <= d1:
            continue
        for j in range(i, min(i + 288 * 3, len(b) - 288 * 6)):
            x = P.bar_utc(b[j]["t"] + 300)
            if style == "swing":
                ny = x.astimezone(NY)
                if ny.weekday() < 4 and ny.hour == 15 and ny.minute == 45:
                    out.append((sym, j))
                    break
            else:
                m = x.astimezone(MAD)
                if m.weekday() < 5 and 8 <= m.hour < 19 and rnd.random() < 0.03:
                    out.append((sym, j))
                    break
    return out


def m_packet(sym, i, style, f, masked):
    b = m_feed(sym)
    t = b[i]["t"]
    now = P.bar_utc(t + 300)
    lines = []
    for tf, k in PACKS[style]:
        per = 288 if tf == "D1" else P.TF_SECONDS.get(tf, 300) // 300
        rows = P.resample(b[max(0, i + 1 - per * (k + 3)):i + 1], tf, t, forming_at=t + 300)
        rows = (rows[1:] if tf != "M5" else rows)[-k:]
        lines.append(f"{tf}, last {len(rows)} closed bars (open high low close):")
        for r in rows:
            u = P.bar_utc(r["t"])
            days = (now.date() - u.date()).days
            label = (f"D-{days} {u:%a %H:%M}" if masked else f"{u:%Y-%m-%d %H:%M}")
            lines.append(f"{label} {r['o'] * f:.6g} {r['h'] * f:.6g} {r['l'] * f:.6g} {r['c'] * f:.6g}")
    return "\n".join(lines), now


def m_prompt(sym, i, style, f, masked, playbook):
    pk, now = m_packet(sym, i, style, f, masked)
    sp = R.spec(sym)["spread_price_snapshot"] * f
    who = (f"The instrument is a {UNIVERSE[sym]} market; its name and the dates are hidden and prices are rescaled: read the chart, nothing else."
           if masked else f"The instrument is {sym}.")
    now_s = now.strftime("%a %H:%M UTC") if masked else now.strftime("%Y-%m-%d %H:%M UTC")
    return (f"You are a discretionary trader on a prop evaluation. Every trade risks the same fixed amount, so what matters is the R you make per "
            f"trade after costs. {ASK[style]}\n{who} Now: {now_s}. The last bar has just closed; a market order fills at the next bar's open, "
            f"about the last close. Spread: {sp:.5g}.\n\n{pk}\n\n" + (f"YOUR PLAYBOOK, from your own graded trades:\n{playbook}\n\n" if playbook else "")
            + "Decide: BUY, SELL or SKIP. A skip is a normal outcome. If you trade, put the stop and the target at real structure.\n"
            'Answer with ONLY one JSON object: {"action":"BUY|SELL|SKIP","sl":number or null,"tp":number or null,"setup":"a few words","reason":"one line"}')


def m_walk(sym, i, sg, stop_d, tp_d, style):
    """R on the feed from the next bar's open, half spread each way, flat before the weekend."""
    b, hs = m_feed(sym), R.spec(sym)["spread_price_snapshot"] / 2
    e = b[i + 1]["o"] + sg * hs
    sl, tp = e - sg * stop_d, e + sg * tp_d
    for x in b[i + 1:i + 1 + HORIZON[style]]:
        if (x["l"] - hs <= sl) if sg > 0 else (x["h"] + hs >= sl):
            return -1.0
        if (x["h"] - hs >= tp) if sg > 0 else (x["l"] + hs <= tp):
            return tp_d / stop_d
        u = P.bar_utc(x["t"])
        if u.weekday() == 4 and u.hour >= 20:
            return sg * (x["c"] - sg * hs - e) / stop_d
    return sg * (x["c"] - sg * hs - e) / stop_d


def m_call(text, config_dir):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(config_dir)
    wd = MASK_OUT / "wd"
    wd.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(["claude", "-p", "--output-format", "json", "--model", "sonnet", "--effort", "medium", "--tools", "",
                        "--strict-mcp-config", "--max-budget-usd", "0.2"], input=text, capture_output=True, text=True, cwd=wd, timeout=240, env=env)
    e = json.loads(p.stdout)
    t = e.get("result") or ""
    if e.get("is_error") or "limit" in t.lower()[:80]:
        raise RuntimeError(t[:200])
    m = re.search(r"\{.*\}", t, re.S)
    return (json.loads(m.group(0)) if m else {"action": "?"}), float(e.get("total_cost_usd") or 0)


def m_run(a):
    MASK_OUT.mkdir(parents=True, exist_ok=True)
    path = MASK_OUT / f"{a.name}.jsonl"
    done = {json.loads(x)["k"] for x in open(path)} if path.exists() else set()
    book = Path(a.playbook).read_text() if a.playbook else ""
    rnd = random.Random(a.seed)
    jobs = []
    for k, (sym, i) in enumerate(m_moments(a.style, a.n, a.seed, a.from_, a.to)):
        f = 10 ** rnd.uniform(-0.7, 0.7)
        if k not in done:
            jobs.append((k, sym, i, 1.0 if a.dated else f))
    spent = [0.0]

    def one(job):
        k, sym, i, f = job
        if spent[0] >= a.max_usd:
            return None
        ans, cost = m_call(m_prompt(sym, i, a.style, f, not a.dated, book), a.config_dir)
        spent[0] += cost
        rec = {"k": k, "symbol": sym, "i": i, "utc": P.bar_utc(m_feed(sym)[i]["t"]).strftime("%Y-%m-%d %H:%M"), "style": a.style, "action": ans.get("action"),
               "setup": ans.get("setup"), "reason": ans.get("reason"), "cost": cost}
        if ans.get("action") in ("BUY", "SELL") and ans.get("sl") and ans.get("tp"):
            sg = 1 if ans["action"] == "BUY" else -1
            last = m_feed(sym)[i]["c"]
            sd, td = (last - float(ans["sl"]) / f) * sg, (float(ans["tp"]) / f - last) * sg
            if sd > 0 and td > 0:
                rec.update({"rr": round(td / sd, 2), "stop_pct": round(sd / last * 100, 3), "R": round(m_walk(sym, i, sg, sd, td, a.style), 3),
                            "mirror": round(m_walk(sym, i, -sg, sd, td, a.style), 3)})
        return rec
    with cf.ThreadPoolExecutor(a.workers) as ex, open(path, "a") as out:
        for rec in ex.map(one, jobs):
            if rec:
                out.write(json.dumps(rec) + "\n")
                out.flush()
    print(a.name, "spent", round(spent[0], 2))


def m_stat(xs):
    if not xs:
        return "n=0"
    m = sum(xs) / len(xs)
    s = (sum((x - m) ** 2 for x in xs) / max(1, len(xs) - 1)) ** 0.5
    return f"n={len(xs)} mean {m:+.2f} se {s / len(xs) ** 0.5:.2f} win {sum(x > 0 for x in xs) / len(xs):.0%}"


def m_report(names):
    for nm in names:
        rows = [json.loads(x) for x in open(MASK_OUT / f"{nm}.jsonl")]
        t = [r for r in rows if "R" in r]
        print(f"{nm}: {len(rows)} moments, {len(t)} trades ({len(t) / max(1, len(rows)):.0%}), ${sum(r.get('cost', 0) for r in rows):.2f}")
        print("   its side  ", m_stat([r["R"] for r in t]))
        print("   mirror    ", m_stat([r["mirror"] for r in t]))
        print("   side skill", m_stat([r["R"] - r["mirror"] for r in t]))


def masked_main(argv) -> int:
    ap = argparse.ArgumentParser(prog="scout_eval.py masked")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--name", required=True)
    r.add_argument("--style", choices=list(PACKS), required=True)
    r.add_argument("--n", type=int, default=40)
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--dated", action="store_true")
    r.add_argument("--from", dest="from_", default="2025-02-01")
    r.add_argument("--to", default="2026-09-01")
    r.add_argument("--playbook")
    r.add_argument("--max-usd", type=float, default=2.0)
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--config-dir", default="~/.claude-a")
    p = sub.add_parser("report")
    p.add_argument("names", nargs="+")
    a = ap.parse_args(argv)
    m_run(a) if a.cmd == "run" else m_report(a.names)
    return 0


def main(argv=None) -> int:
    if (sys.argv[1:] if argv is None else argv)[:1] == ["masked"]:
        return masked_main((sys.argv[1:] if argv is None else argv)[1:])
    ap = argparse.ArgumentParser(prog="scout_eval.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--run", required=True)
    b.add_argument("--n", type=int, default=30)
    b.add_argument("--from", dest="frm", default="2025-07-01")
    b.add_argument("--to", default="2025-09-30")
    b.add_argument("--seed", type=int, default=42)
    r = sub.add_parser("run")
    r.add_argument("--run", required=True)
    r.add_argument("--arms", default="sonnet,haiku,haiku_lib")
    r.add_argument("--workers", type=int, default=4)
    r.add_argument("--guard-account", help="castl account name to watch, e.g. account_2 (interleaved run)")
    r.add_argument("--max-5h", type=float, default=88.0)
    r.add_argument("--max-week", type=float, default=97.0)
    rp = sub.add_parser("report")
    rp.add_argument("--run", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "build":
        build(a.run, a.n, a.frm, a.to, a.seed)
    elif a.cmd == "run":
        if a.guard_account:
            run_interleaved(a.run, a.arms.split(","), a.guard_account, a.max_5h, a.max_week)
        else:
            run(a.run, a.arms.split(","), a.workers)
    else:
        print(report(a.run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
