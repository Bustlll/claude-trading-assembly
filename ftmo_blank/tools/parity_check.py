"""PARITY CHECK: are TRAINING (paper replay) and LIVE (real account) still the same trading logic?

Two code paths do the same job: tools/paper.py + driver.py turn() + agent_code/routing.py for training,
tools/live_loop.py + driver.py live-tick + tools/live_routing.py for live. Whatever training teaches is
only worth something if live runs the same desk. This drives BOTH implementations with the same synthetic
market and the same clock, and compares what they do -- not their source text:

  scan.*     the move screen: same candidates and % from the same bars; thresholds, lookback, cap
  window     the trading window: same minutes in and out, a summer and a winter week
  decisions  when a decision turn fires over a week
  scouting   how often one symbol that keeps moving all day wakes a scout
  routing.*  which kind each twin event wakes, and what the scout prompt carries
  kinds.*    each training kind vs its live twin: model, effort, budget, journal/alert reach, spawn reach
  format.*   what the workers' tools print (bars, board)
  info.*     what lands IN each twin worker's prompt (2026-10-18): the same candles from the same market,
             the same board thread, the lessons on the same path, the same scout task, the same pre-order
             checklist and trader guide, the lessons verb on every twin kind (tools/desk_shared.py)

Every check is SAME or DRIFT. A DRIFT is either listed in KNOWN_DRIFT (with why, and who decided) or it
is new -- and tests/test_parity.py fails on any new drift AND on a known one that has since gone away,
so the list stays the true picture.

  python3 tools/parity_check.py            the table
  python3 tools/parity_check.py --json     machine-readable
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as dt
import io
import json
import sys
import tempfile
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "tools"))
sys.path.insert(0, str(HOME))
sys.path.insert(0, str(HOME / "tests"))
sys.path.insert(0, str(HOME / "agent_code"))

# Drift that exists ON PURPOSE or is waiting for a decision. Remove an entry when the two paths are aligned;
# add one only with the reason. tests/test_parity.py keeps this honest in both directions.
KNOWN_DRIFT = {
    "routing.vocabulary": "training's scout carries no vocabulary block (2026-11-01, operator's trim; ablation in todos/009: "
                          "it and the checklist cut hand-offs, a scout places no order). Live is unchanged: its scouts keep both",
    "info.entry_rules": "training's scout carries no pre-order checklist, the trader it hands to does (2026-11-01, same "
                        "reason as routing.vocabulary); live is unchanged",
    "kinds.trade.spawn": "training's trade spawns evolve/write/review; live has no evolve while real money "
                         "runs (guard/policy.py design, 2026-10-11)",
    "kinds.review.spawn": "training's review may spawn evolve; live has no evolve (design, 2026-10-11)",
    "kinds.trade_symbol.spawn": "training symbol traders lost the review spawn (2026-10-18: always found the "
                                "order unfilled); live traders keep it -- live clock is real, a review can see a fill",
}


def _row(name, same, training, live, note=""):
    return {"check": name, "status": "SAME" if same else "DRIFT", "training": training, "live": live, "note": note}


# ---- the move screen ------------------------------------------------------------------------------
def _synthetic(now_utc: dt.datetime):
    """Three symbols, 30 M5 bars ending at now: EURUSD +0.45% in the last hour, XAUUSD -0.6%, GBPUSD flat."""
    import live_broker as LB
    series = {"EURUSD": (1.1000, 1.1050), "XAUUSD": (3300.0, 3280.0), "GBPUSD": (1.3000, 1.3001)}
    bars = {}
    for sym, (a, b) in series.items():
        rows = []
        for k in range(30):
            t = now_utc - dt.timedelta(minutes=5 * (30 - k))
            c = a if k < 17 else a + (b - a) * (k - 16) / 13
            rows.append({"t": LB.utc_to_broker_epoch(t), "o": c, "h": c, "l": c, "c": c})
        bars[sym] = rows
    return bars


def check_scan() -> list[dict]:
    import live_loop as LL
    import paper as P
    from live_fakes import FakeMT5
    now = dt.datetime(2026, 1, 7, 11, 0, tzinfo=dt.timezone.utc)   # a Wednesday, in the window
    bars = _synthetic(now)
    tmp = Path(tempfile.mkdtemp(prefix="parity_"))
    (tmp / "feed").mkdir()
    (tmp / "state").mkdir()
    for sym, rows in bars.items():
        with open(tmp / "feed" / f"{sym}_M5.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t_broker", "o", "h", "l", "c", "session"])
            for r in rows:
                w.writerow([r["t"], r["o"], r["h"], r["l"], r["c"], "day"])
    p = P.Paper(home=tmp)
    p.s["cursor"] = len(p.timeline)          # t_now = the last bar, closed at `now`
    train = [(c["symbol"], c["pct_move"]) for c in p.scan_candidates()]
    rates = {(s, FakeMT5.TIMEFRAME_M5): [{"time": r["t"], "open": r["o"], "high": r["h"], "low": r["l"],
                                         "close": r["c"]} for r in rows] for s, rows in bars.items()}
    m = FakeMT5(rates=rates)
    with contextlib.redirect_stdout(io.StringIO()):
        live = [(c["symbol"], c["pct_move"]) for c in LL.scan_candidates(m, now, [])]
    return [
        _row("scan.candidates", train == live, train, live, "same bars, same clock: who is flagged and by how much"),
        _row("scan.threshold_pct", P.SCAN_THRESHOLD_PCT == LL.SCAN_THRESHOLD_PCT, P.SCAN_THRESHOLD_PCT, LL.SCAN_THRESHOLD_PCT),
        _row("scan.lookback_m5", P.SCAN_EVERY_BARS == LL.SCAN_LOOKBACK_M5, P.SCAN_EVERY_BARS, LL.SCAN_LOOKBACK_M5),
        _row("scan.max_per_event", P.SCAN_MAX == LL.SCAN_MAX, P.SCAN_MAX, LL.SCAN_MAX),
        _row("scan.decision_movers", P.DECISION_MOVERS == LL.MOVERS_IN_DECISION, P.DECISION_MOVERS, LL.MOVERS_IN_DECISION),
    ]


# ---- the clock --------------------------------------------------------------------------------------
def _week(start: dt.datetime):
    t = start
    while t < start + dt.timedelta(days=7):
        yield t
        t += dt.timedelta(minutes=5)


def _paper_t(now: dt.datetime) -> int:
    """paper.py judges a bar by its CLOSE: the bar that closes at `now` opened 5 minutes earlier."""
    import live_broker as LB
    return LB.utc_to_broker_epoch(now - dt.timedelta(minutes=5))


def check_clock() -> list[dict]:
    import live_loop as LL
    import paper as P
    out = []
    mism, inside = [], 0
    for start in (dt.datetime(2026, 1, 5, tzinfo=dt.timezone.utc), dt.datetime(2026, 7, 6, tzinfo=dt.timezone.utc)):
        for now in _week(start):
            a, b = P.in_train_window(_paper_t(now)), LL.in_window(now)
            inside += a
            if a != b:
                mism.append(now.strftime("%a %Y-%m-%d %H:%M UTC"))
    out.append(_row("window", not mism, f"{inside} in-window 5-min ticks over two weeks",
                    f"{len(mism)} ticks disagree" + (f", first {mism[:3]}" if mism else ""),
                    "the same minutes count as the trading window (DST-aware, Europe/Madrid)"))

    # decisions over one week: when does a decision turn fire?
    week = dt.datetime(2026, 1, 5, tzinfo=dt.timezone.utc)
    live_times, slot = [], None
    for now in _week(week):
        s = LL.decision_slot(now)
        if s and s != slot:
            live_times.append(now)
        slot = s if s else slot
    tslot, train_times = None, []
    for now in _week(week):
        sl = P.train_decision_slot(_paper_t(now))      # the replay's own clock function (paper.py _clock)
        if sl and sl != tslot:
            train_times.append(now)
        tslot = sl if sl else tslot
    fmt = lambda ts: f"{len(ts)}/week, first day at " + ", ".join(
        t.astimezone(P.TRAIN_TZ).strftime("%H:%M") for t in ts if t.date() == ts[0].date()) if ts else "none"
    out.append(_row("decisions", [t for t in train_times] == live_times, fmt(train_times), fmt(live_times),
                    "decision turns over one week (Madrid times)"))

    # scouting: one symbol that keeps moving all day -- how many scout wakes?
    day = dt.datetime(2026, 1, 7, tzinfo=dt.timezone.utc)
    live_wakes, st = 0, {"flagged": [], "flagged_at": {}}
    for now in (day + dt.timedelta(minutes=5 * k) for k in range(288)):
        if not LL.in_window(now):
            continue
        flagged = LL._live_flags(st, now)
        if "EURUSD" not in flagged:
            LL._flag(st, ["EURUSD"], now)
            live_wakes += 1
    train_wakes, flags = 0, {}
    for now in (day + dt.timedelta(minutes=5 * k) for k in range(288)):
        t = _paper_t(now)
        if not P.in_train_window(t):
            continue
        flags = P.expire_flags(flags, t + 300)          # the replay's own re-flag rule (paper.py _clock)
        if "EURUSD" not in flags:
            flags["EURUSD"] = t + 300
            train_wakes += 1
    out.append(_row("scouting", train_wakes == live_wakes, f"{train_wakes} scout wake(s)/day",
                    f"{live_wakes} scout wake(s)/day", "one symbol moving >= threshold all day long"))
    return out


# ---- routing --------------------------------------------------------------------------------------------
def check_routing() -> list[dict]:
    import routing as TR
    import live_routing as LR
    out = []
    pairs = [("setup_candidate", "live_setup_candidate", {"candidates": [{"symbol": "EURUSD", "pct_move": 0.4, "bars": 12}]}),
             ("risk_warning", "live_risk_warning", {"kind": "daily", "used_pct_of_limit": 61}),
             ("day_closed", "live_day_closed", {"traded": True, "journal_notes_today": 1, "journal_notes": 1}),
             ("manage_wake", "live_manage", {"symbol": "XAUUSD", "side": "buy", "id": 1, "ticket": 1, "entry": 3300.0,
                                             "sl": 3290.0, "sl0": 3290.0, "lots": 0.1, "opened_utc": "2026-01-07 08:00",
                                             "triggers": [{"code": "R+0.5", "why": "x"}], "info": {}})]
    twin = lambda k: (k or "").replace("live_", "").replace("live", "trade") if k else None
    for tev, lev, payload in pairs:
        try:
            tk = (TR.route(tev, dict(payload)) or (None,))[0]
        except Exception as exc:
            tk = f"error {exc}"
        try:
            lk = (LR.route(lev, {**payload, "snapshot": {}}) or (None,))[0]
        except Exception as exc:
            lk = f"error {exc}"
        out.append(_row(f"routing.{tev}", twin(lk) == tk, tk, lk, "which kind the twin events wake"))
    tk = TR.route("day_closed", {"traded": False, "journal_notes_today": 1})
    try:
        lk = LR.route("live_day_closed", {"traded": False, "journal_notes": 1, "snapshot": {}})
    except Exception as exc:
        lk = f"error {exc}"
    out.append(_row("routing.untraded_day_review", (tk is None) == (lk is None), tk and tk[0], lk and lk[0],
                    "a day with a journal note but no trade"))
    _, tp = TR.route("setup_candidate", {"candidates": [{"symbol": "EURUSD", "pct_move": 0.4, "bars": 12}],
                                         "bars_text": "THE CANDLES, ATTACHED ...", "traded_days_total": 0, "window_day": 6})
    try:
        _, lp = LR.route("live_setup_candidate", {"candidates": [{"symbol": "EURUSD", "pct_move": 0.4, "bars": 12}],
                                                  "bars_text": "THE CANDLES, ATTACHED ...", "snapshot": {}})
    except Exception as exc:
        lp = f"error {exc}"
    out.append(_row("routing.scout_forced_handoff", ("HAND-OFF REQUIRED" in tp) == ("HAND-OFF REQUIRED" in lp),
                    "HAND-OFF REQUIRED" in tp, "HAND-OFF REQUIRED" in lp, "a scout must never be forced to hand off"))
    out.append(_row("routing.scout_candles", ("THE CANDLES, ATTACHED" in tp) == ("THE CANDLES, ATTACHED" in lp) == True,
                    "THE CANDLES, ATTACHED" in tp, "THE CANDLES, ATTACHED" in lp, "the movers' candles ride in the scout prompt"))
    out.append(_row("routing.alerts", ("alert --symbol" in tp) == ("alert --symbol" in lp),
                    "alert --symbol" in tp, "alert --symbol" in lp, "a scout can plant a price alert"))
    import vocab
    block = vocab.prompt_block()
    _, t_reassess = TR.route("turn_reassess", {"snapshot": {"bar_index": 0, "bars_total": 0, "utc": "-", "epoch": 0,
                                                            "window_index": 0, "windows_per_epoch": 1, "window_day": 1,
                                                            "window_business_days": 10, "attempt": 1, "pass_rate": None,
                                                            "attempts_total": 0}, "goals": []})
    out.append(_row("routing.vocabulary", block in tp and block in t_reassess and block in lp,
                    f"scout {block in tp}, decider {block in t_reassess}", f"scout {block in lp}",
                    "the same desk-vocabulary block, word for word, in both desks' prompts"))
    t_dec = "trade" in __import__("guard.policy", fromlist=["KINDS"]).KINDS["scout"]["may_spawn"]
    l_dec = "live" in __import__("guard.policy", fromlist=["LIVE_KINDS"]).LIVE_KINDS["live_scout"]["may_spawn"]
    out.append(_row("routing.scout_to_decider", t_dec == l_dec, t_dec, l_dec, "a scout may wake the decider for any symbol"))
    return out


# ---- kinds ------------------------------------------------------------------------------------------------
def check_kinds() -> list[dict]:
    from guard import policy as PO
    out = []
    twins = [("trade", "live"), ("scout", "live_scout"), ("chart", "live_chart"), ("timing", "live_timing"),
             ("review", "live_review"), ("risk", "live_risk"), ("manage", "live_manage"), ("vault", "live_vault")]
    twins += [(f"trade_{s.lower().replace('.', '_')}", f"live_trade_{s.lower().replace('.', '_')}")
              for s in PO.SYMBOL_TRADERS]
    to_train = lambda k: k.replace("live_trade_", "trade_").replace("live_", "") if k != "live" else "trade"
    seen = set()
    for tk, lk in twins:
        a, b = PO.KINDS.get(tk), PO.LIVE_KINDS.get(lk)
        group = "trade_symbol" if tk.startswith("trade_") else tk
        if a is None or b is None:
            out.append(_row(f"kinds.{group}.exists", False, a is not None, b is not None, f"{tk} vs {lk}"))
            continue
        for field in ("model", "effort", "budget_usd"):
            name = f"kinds.{group}.{field}"
            if (name, a.get(field), b.get(field)) in seen:
                continue
            seen.add((name, a.get(field), b.get(field)))
            out.append(_row(name, a.get(field) == b.get(field), a.get(field), b.get(field), f"{tk} vs {lk}"))
        tv = any("vocab.py" in r for r in a.get("allow", []))
        lv = any("vocab.py" in r for r in b.get("allow", []))
        if f"kinds.{group}.vocab" not in {r["check"] for r in out}:
            out.append(_row(f"kinds.{group}.vocab", tv == lv == True, tv, lv, f"{tk} vs {lk}: may look up the vocabulary"))
        for verb in ("journal", "alert", "close", "modify", "widen", "watch"):
            ta = any(f" {verb}" in r for r in a.get("allow", []))
            la = any(f" {verb}" in r for r in b.get("allow", []))
            name = f"kinds.{group}.{verb}"
            if name not in {r["check"] for r in out}:
                out.append(_row(name, ta == la, ta, la, f"{tk} vs {lk}: may run {verb}"))
        tc = any("consult.py training" in r for r in a.get("allow", []))
        lc = any("consult.py live" in r for r in b.get("allow", []))
        if f"kinds.{group}.consult" not in {r["check"] for r in out}:
            out.append(_row(f"kinds.{group}.consult", tc == lc, tc, lc, f"{tk} vs {lk}: may consult chart/timing now"))
        ts = {k for k in a.get("may_spawn", []) if k not in ("reflect",)}
        ls = {to_train(k) for k in b.get("may_spawn", []) if k not in ("reflect",)}
        name = f"kinds.{group}.spawn"
        if name not in {r["check"] for r in out}:
            out.append(_row(name, ts == ls, sorted(ts), sorted(ls), f"{tk} vs {lk}, live names mapped to training's"))
    return out


# ---- what the tools print ----------------------------------------------------------------------------------
def check_formats() -> list[dict]:
    import paper as P
    import board as TB
    import live_board as LBd
    rows = [{"utc": "2026-01-07 10:55", "day": "2026-01-07", "tf": "M5", "session": "day",
             "o": 1.1, "h": 1.1, "l": 1.1, "c": 1.1}]
    t_bars = P.bars_text("EURUSD", "M5", rows)
    import live_market as LM
    l_bars = LM.bars_text("EURUSD", "M5", rows) if hasattr(LM, "bars_text") else json.dumps(rows, indent=1)
    post = [{"utc": "2026-01-07 10:55 UTC", "worker": "scout", "topic": "scan", "text": "x", "symbol": "EURUSD"}]
    t_board = TB.read_text(post)
    l_board = LBd.read_text(post) if hasattr(LBd, "read_text") else json.dumps(post, indent=1)
    def kind(s: str) -> str:
        try:
            json.loads(s)
            return "json"
        except ValueError:
            return "table/lines"
    return [_row("format.bars", kind(t_bars) == kind(l_bars), kind(t_bars), kind(l_bars)),
            _row("format.board", kind(t_board) == kind(l_board), kind(t_board), kind(l_board))]


# ---- what lands in the prompt ------------------------------------------------------------------------------
def _info_market(now: dt.datetime, days: int = 4):
    """Two symbols, `days` of M5 bars ending at `now` (a seeded random walk), with the session each desk
    computes (night = Prague 22:00-07:00): as paper.py's feed and as the broker's M5/M15/H1 rates."""
    import random
    import live_broker as LB
    import live_market as LM
    import paper as P
    rnd = random.Random(18)
    m5 = {}
    for sym, px in (("EURUSD", 1.1), ("XAUUSD", 3300.0)):
        rows, t = [], now - dt.timedelta(days=days)
        while t < now:
            o = px
            c = round(px * (1 + rnd.uniform(-0.0012, 0.0012)), 5)
            h, l = round(max(o, c) * (1 + rnd.uniform(0, 4e-4)), 5), round(min(o, c) * (1 - rnd.uniform(0, 4e-4)), 5)
            tb = LB.utc_to_broker_epoch(t)
            rows.append({"t": tb, "o": o, "h": h, "l": l, "c": c, "session": LM._session(tb)})
            px, t = c, t + dt.timedelta(minutes=5)
        m5[sym] = rows
    far = LB.utc_to_broker_epoch(now + dt.timedelta(days=30))
    rates = {}
    for sym, rows in m5.items():
        for tf, code in (("M5", "TIMEFRAME_M5"), ("M15", "TIMEFRAME_M15"), ("H1", "TIMEFRAME_H1")):
            src = rows if tf == "M5" else P.resample(rows, tf, far)   # the broker's own bars: every bucket, forming one too
            rates[(sym, code)] = [{"time": b["t"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"]}
                                  for b in src]
    return m5, rates


def check_info() -> list[dict]:
    import desk_shared as DS
    import live_broker as LB
    import live_market as LM
    import paper as P
    import routing as TR
    import live_routing as LR
    import driver as D
    from guard import policy as PO
    from live_fakes import FakeMT5
    out = []
    now = dt.datetime(2026, 1, 7, 11, 0, tzinfo=dt.timezone.utc)   # a Wednesday, top of the hour: a scan moment
    m5, rates = _info_market(now)
    tmp = Path(tempfile.mkdtemp(prefix="parity_info_"))
    (tmp / "feed").mkdir()
    (tmp / "state").mkdir()
    for sym, rows in m5.items():
        with open(tmp / "feed" / f"{sym}_M5.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t_broker", "o", "h", "l", "c", "session"])
            for r in rows:
                w.writerow([r["t"], r["o"], r["h"], r["l"], r["c"], r["session"]])
    p = P.Paper(home=tmp)
    p.s["cursor"] = len(p.timeline)          # the replay stopped on the bar that closes at `now`
    fake = {k: v for k, v in rates.items()}
    m = FakeMT5(rates={(s, getattr(FakeMT5, code)): v for (s, code), v in fake.items()})
    saved = LB._now_utc
    LB._now_utc = lambda: now                 # the live desk reads its clock here (live_market, live_board)
    try:
        syms = ["XAUUSD", "EURUSD"]
        # two moments: the top of the hour (the M15 and H1 bars that just closed must be there) and 5 minutes
        # before it (the 10:45 M15 and 10:00 H1 buckets are still forming: neither desk may show them)
        diffs, sizes = [], []
        for back, label in ((0, "11:00"), (1, "10:55")):
            p.s["cursor"] = len(p.timeline) - back   # the timeline holds one entry per M5 timestamp
            clock = now - dt.timedelta(minutes=5 * back)
            LB._now_utc = lambda clock=clock: clock
            t_pack = DS.packets(lambda s, tf, n: p.recent_bars(s, n, tf), syms, "BARS")
            l_pack = DS.packets(lambda s, tf, n: LM.bars(s, n, tf, m=m), syms, "BARS")
            sizes.append(f"{label}: {t_pack.count(chr(10))}/{l_pack.count(chr(10))} lines")
            first = next((f"{a!r} vs {b!r}" for a, b in zip(t_pack.splitlines(), l_pack.splitlines()) if a != b),
                         "" if t_pack == l_pack else "lengths differ")
            if first:
                diffs.append(f"{label} {first}")
        p.s["cursor"] = len(p.timeline)
        LB._now_utc = lambda: now
        out.append(_row("info.scout_candles", not diffs, "; ".join(sizes), "; ".join(diffs) or "identical",
                        "same market, same moment: the candles attached to a scout, line for line"))
        tp_views = D.train_views({"type": "setup_candidate", "candidates": [{"symbol": s} for s in syms],
                                  "utc": "2026-01-07 10:55"}, p, {"utc": "2026-01-07 10:55", "prague_day": "2026-01-07"})
        lp_views = D.live_views({"type": "live_setup_candidate", "candidates": [{"symbol": s} for s in syms]},
                                {"utc": "2026-01-07 11:00 UTC"}, m)
        out.append(_row("info.scout_view", sorted(tp_views) == sorted(lp_views), sorted(tp_views), sorted(lp_views),
                        "the parts a scout prompt carries: candles, today's thread, lessons"))
    finally:
        LB._now_utc = saved
    # the same posts make the same thread on both desks (each desk's own day rule)
    import kstore as K
    posts_t = [{"utc": "2026-01-07 08:10 UTC", "worker": "scout", "topic": "pre_trade", "symbol": "XAUUSD", "text": "S1 BUY @3302"},
               {"utc": "2026-01-06 15:00 UTC", "worker": "trade", "topic": "decision", "symbol": "XAUUSD", "text": "yesterday"}]
    posts_l = [{**x, "day": K.rec_day(x)} for x in posts_t]
    t_thr = DS.thread(posts_t, "2026-01-07", "XAUUSD", K.rec_day)
    l_thr = DS.thread(posts_l, "2026-01-07", "XAUUSD", lambda x: x.get("day"))
    out.append(_row("info.thread", t_thr == l_thr and "yesterday" not in t_thr, t_thr.count("\n") + 1, l_thr.count("\n") + 1,
                    "a symbol's board thread today, from the same posts"))
    # the prompts carry every part, and the same scout task word for word
    view = {"bars_text": "THE CANDLES, ATTACHED x", "threads_text": "TODAY ON THE BOARD x", "lessons_text": "LESSONS ON THIS PATH x"}
    cand = {"candidates": [{"symbol": "EURUSD", "pct_move": 0.4, "bars": 12}]}
    _, tp = TR.route("setup_candidate", {**cand, **view, "traded_days_total": 0, "window_day": 6})
    _, lp = LR.route("live_setup_candidate", {**cand, **view, "snapshot": {}})
    parts = lambda t: [k for k in ("THE CANDLES, ATTACHED", "TODAY ON THE BOARD", "LESSONS ON THIS PATH") if k in t] + \
        (["SCOUT_TASK"] if DS.SCOUT_TASK in t else [])
    out.append(_row("info.scout_prompt", parts(tp) == parts(lp) and len(parts(tp)) == 4, parts(tp), parts(lp),
                    "the scout prompt carries the view and the shared task text"))
    snap_t = {"bar_index": 0, "bars_total": 0, "utc": "-", "epoch": 0, "window_index": 0, "windows_per_epoch": 1,
              "window_day": 1, "window_business_days": 10, "attempt": 1, "pass_rate": None, "attempts_total": 0}
    _, t_dec = TR.route("turn_reassess", {"snapshot": snap_t, "goals": [], "lessons_text": "LESSONS ON THIS PATH x"})
    _, l_dec = LR.route("live_reassess", {"snapshot": {}, "lessons_text": "LESSONS ON THIS PATH x"})
    out.append(_row("info.decider_task", DS.DECIDER_TASK in t_dec and DS.DECIDER_TASK in l_dec,
                    DS.DECIDER_TASK in t_dec, DS.DECIDER_TASK in l_dec, "the decision turn's principles, word for word"))
    out.append(_row("info.decider_lessons", ("LESSONS ON THIS PATH" in t_dec) == ("LESSONS ON THIS PATH" in l_dec) == True,
                    "LESSONS ON THIS PATH" in t_dec, "LESSONS ON THIS PATH" in l_dec, "the decision turn carries its path's lessons"))
    _, t_rev = TR.route("trade_closed", {"symbol": "XAUUSD", "reason": "sl", "lessons_text": "LESSONS ON THIS PATH x"})
    _, l_rev = LR.route("live_trade_closed", {"symbol": "XAUUSD", "reason": "sl", "lessons_text": "LESSONS ON THIS PATH x",
                                              "snapshot": {}})
    out.append(_row("info.review_lessons", t_rev.count("LESSONS ON THIS PATH") == l_rev.count("LESSONS ON THIS PATH") == 1,
                    t_rev.count("LESSONS ON THIS PATH"), l_rev.count("LESSONS ON THIS PATH"),
                    "a trade review carries the trade's path, once (not again inside its record)"))
    # the views the drivers build for each twin event
    for tev, lev in (("trade_closed", "live_trade_closed"),):
        rec = {"symbol": "XAUUSD", "why_open": "S1 BUY", "opened_utc": "2026-01-07 09:00"}
        a, b = sorted(D.train_views({"type": tev, **rec}, p, {})), sorted(D.live_views({"type": lev, **rec}, {}, None))
        out.append(_row(f"info.view.{tev}", a == b, a, b, "what the driver attaches to this event"))
    # the checklist and the trader guide: in every context, in every symbol trader's own notes
    core = DS.ENTRY_RULES_CORE
    lctx = LR.live_context({"snapshot": {}})
    out.append(_row("info.entry_rules", core in TR.CONTEXT and core in TR.SCOUT_CONTEXT and core in lctx,
                    f"CONTEXT {core in TR.CONTEXT}, SCOUT {core in TR.SCOUT_CONTEXT}", f"live_context {core in lctx}",
                    "the pre-order checklist, word for word"))
    guide = lambda sym, board, journal: DS.trader_guide(sym, board, journal)
    t_ok = all(core in PO.KINDS[f"trade_{s.lower().replace('.', '_')}"]["notes"]
               and guide(s, f"python3 {PO.BOARD}", f"python3 {PO.PAPER} journal") in PO.KINDS[f"trade_{s.lower().replace('.', '_')}"]["notes"]
               for s in PO.SYMBOL_TRADERS)
    l_ok = all(core in PO.LIVE_KINDS[f"live_trade_{s.lower().replace('.', '_')}"]["notes"]
               and guide(s, f"python3 {PO.LIVE_BOARD}", f"python3 {PO.LIVE_BROKER} journal") in
               PO.LIVE_KINDS[f"live_trade_{s.lower().replace('.', '_')}"]["notes"] for s in PO.SYMBOL_TRADERS)
    out.append(_row("info.trader_notes", t_ok == l_ok == True, t_ok, l_ok, "every symbol trader carries the checklist and the guide"))
    tl = {k for k, v in PO.KINDS.items() if any(f"{PO.BOARD} lessons" in r for r in v.get("allow", []))}
    ll = {k for k, v in PO.LIVE_KINDS.items() if any(f"{PO.LIVE_BOARD} lessons" in r for r in v.get("allow", []))}
    twins = [("trade", "live"), ("scout", "live_scout"), ("chart", "live_chart"), ("timing", "live_timing"),
             ("review", "live_review"), ("risk", "live_risk"), ("manage", "live_manage")] + \
            [(f"trade_{s.lower().replace('.', '_')}", f"live_trade_{s.lower().replace('.', '_')}") for s in PO.SYMBOL_TRADERS]
    miss = [f"{a}/{b}" for a, b in twins if (a in tl) != (b in ll)]
    out.append(_row("info.lessons_verb", not miss, f"{len(tl)} kinds", f"{len(ll)} kinds", "missing on one side: "
                    + (", ".join(miss) or "none")))
    return out


def check_alerts_and_trader() -> list[dict]:
    """The same alert set at the same moment fires on the same bar on both desks (tools/alerts_core.py via
    paper.py and live_alerts.py); a woken trader's symbol file carries the same candles; both engines attach
    it to a spawned trader; a graded pass reaches the day review on both."""
    import bisect
    import desk_shared as DS
    import driver as D
    import live_alerts as LA
    import live_broker as LB
    import live_loop as LL
    import live_market as LM
    import paper as P
    import routing as TR
    import live_routing as LR
    from live_fakes import FakeMT5
    out = []
    end = dt.datetime(2026, 1, 7, 11, 0, tzinfo=dt.timezone.utc)
    m5, rates = _info_market(end)
    start = dt.datetime(2026, 1, 7, 8, 0, tzinfo=dt.timezone.utc)   # alerts set at 08:00 (bar 07:55 just closed)
    tmp = Path(tempfile.mkdtemp(prefix="parity_alerts_"))
    for d in ("feed", "state", "live/state"):
        (tmp / d).mkdir(parents=True)
    for sym, rows in m5.items():
        with open(tmp / "feed" / f"{sym}_M5.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t_broker", "o", "h", "l", "c", "session"])
            for r in rows:
                w.writerow([r["t"], r["o"], r["h"], r["l"], r["c"], r["session"]])
    p = P.Paper(home=tmp)
    t_set = LB.utc_to_broker_epoch(start - dt.timedelta(minutes=5))
    p.s["cursor"] = bisect.bisect_right(p.timeline, t_set)
    p.s["day"] = P.day_key(t_set)
    for sym, rows in m5.items():
        p.s["last_close"][sym] = next(r["c"] for r in reversed(rows) if r["t"] <= t_set)
    # three conditions, levels taken from the path that follows (so they can fire), identical on both desks
    plans = []
    for sym, when, frac in (("XAUUSD", "close", 0.6), ("EURUSD", "reject", 0.5), ("EURUSD", "hold", 0.3)):
        ref = p.s["last_close"][sym]
        later = [r for r in m5[sym] if t_set < r["t"] <= LB.utc_to_broker_epoch(end)]
        hi, lo = max(r["h"] for r in later), min(r["l"] for r in later)
        up = (hi - ref) >= (ref - lo)
        lvl = round(ref + frac * (hi - ref), 5) if up else round(ref - frac * (ref - lo), 5)
        plans.append((sym, when, lvl if up else None, None if up else lvl))
    t_res = [p.set_alert(sym, a, b, f"parity plan {when} {sym}", when=when, tf="M15", nbars=2)
             for sym, when, a, b in plans]

    class ClockFake(FakeMT5):
        clock_b = 0

        def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
            rows = [r for r in self._rates.get((symbol, timeframe), []) if r["time"] <= self.clock_b]
            return rows[max(0, len(rows) - count):]

    fake = ClockFake(rates={(s, getattr(FakeMT5, code)): v for (s, code), v in rates.items()})
    saved = (LB._now_utc, LB.LEDGER_PATH)
    LB.LEDGER_PATH = tmp / "live" / "state" / "live_ledger.jsonl"   # never the real live ledger
    live_hits = []
    try:
        clock = start
        LB._now_utc = lambda: clock
        fake.clock_b = LB.utc_to_broker_epoch(clock)
        l_res = [LA.set_alert(sym, a, b, f"parity plan {when} {sym}", when=when, tf="M15", nbars=2, m=fake,
                              now=clock, home=tmp / "live") for sym, when, a, b in plans]
        while clock < end:
            clock += dt.timedelta(minutes=5)
            fake.clock_b = LB.utc_to_broker_epoch(clock)
            live_hits += [(e["id"], e["symbol"], e["touched_utc"]) for e in LA.check(fake, clock, LL.in_window,
                                                                                     home=tmp / "live")]
        with contextlib.redirect_stdout(io.StringIO()):
            while p.t_now() is not None and p.t_now() < LB.utc_to_broker_epoch(end - dt.timedelta(minutes=5)):
                p.advance(1)
        train_hits = [(r["id"], r["symbol"], r["touched_utc"]) for r in
                      (json.loads(x) for x in (tmp / "state" / "ledger.jsonl").read_text().splitlines())
                      if r.get("type") == "level_alert"]
        ok_set = [r.get("ok") for r in t_res] == [r.get("ok") for r in l_res]
        out.append(_row("info.alerts", ok_set and train_hits == live_hits and len(train_hits) > 0,
                        f"set {[r.get('ok') for r in t_res]}, fired {train_hits}",
                        f"set {[r.get('ok') for r in l_res]}, fired {live_hits}",
                        "the same alerts, set at the same moment, fire on the same bar on both desks"))
        # the rejection tracker: the same flagged mover, not traded, gets the same forward walk on both desks
        import opportunities as OPP
        LB._now_utc = lambda: end
        fake.clock_b = LB.utc_to_broker_epoch(end)
        p.s["cursor"] = len(p.timeline)
        scan = [{"type": "setup_candidate", "utc": "2026-01-07 07:55 UTC",
                 "candidates": [{"symbol": "XAUUSD", "pct_move": 0.4}, {"symbol": "EURUSD", "pct_move": -0.35}]}]
        day_of = lambda r: "2026-01-07"
        g_t = OPP.grade_day(scan, [], "2026-01-07", day_of, lambda s, tf, n: p.recent_bars(s, n, tf), "x")
        g_l = OPP.grade_day(scan, [], "2026-01-07", day_of, lambda s, tf, n: LM.bars(s, n, tf, m=fake), "x")
        out.append(_row("info.opportunities", g_t == g_l and len(g_t) == 2,
                        [(g["symbol"], g["with_move"], g["fade"], g["ran_on_atr"]) for g in g_t],
                        [(g["symbol"], g["with_move"], g["fade"], g["ran_on_atr"]) for g in g_l],
                        "a rejected mover's forward walk, the same on both desks"))
        # the symbol file: same candles from the same market
        fake.clock_b = LB.utc_to_broker_epoch(end)
        p.s["cursor"] = len(p.timeline)
        t_file = D.train_symbol_file("XAUUSD", "S1 BUY", p)
        l_file = D.live_symbol_file("XAUUSD", "S1 BUY", fake)
        cut = lambda t: t[t.index("CANDLES"):t.index("TODAY ON THE BOARD")].split(":", 1)[1]
        heads = lambda t: [h for h in ("CANDLES", "TODAY ON THE BOARD", "LESSONS ON THIS PATH", "YOUR RECORD IN IT")
                           if h in t]
        out.append(_row("info.trader_view", cut(t_file) == cut(l_file) and heads(t_file) == heads(l_file) and
                        len(heads(t_file)) == 4, heads(t_file), heads(l_file),
                        "a woken or spawned trader's symbol file: same candles, same sections"))
    finally:
        LB._now_utc, LB.LEDGER_PATH = saved
    t_eng, l_eng = D.make_engine(invoker=lambda d: None), D.make_live_engine(invoker=lambda d: None)
    out.append(_row("info.child_view", t_eng.child_view is not None and l_eng.child_view is not None,
                    t_eng.child_view is not None, l_eng.child_view is not None,
                    "a spawned symbol trader gets its symbol file attached on both desks"))
    tp = TR.route("day_closed", {"traded": False, "graded_n": 1, "graded_text": "PASSES TODAY x", "day": "d"})
    lp = LR.route("live_day_closed", {"traded": False, "graded_n": 1, "graded_text": "PASSES TODAY x", "snapshot": {}})
    out.append(_row("info.pass_review", bool(tp and lp and "PASSES TODAY" in tp[1] and "PASSES TODAY" in lp[1]),
                    tp and tp[0], lp and lp[0], "a day with a graded pass gets a review that sees the grades"))
    _, ta = TR.route("level_alert", {"symbol": "XAUUSD", "id": 1, "why": "x", "symbol_file": "SYMBOL FILE x"})
    _, la = LR.route("live_level_alert", {"symbol": "XAUUSD", "id": 1, "why": "x", "symbol_file": "SYMBOL FILE x",
                                          "snapshot": {}})
    _, tc = TR.route("calibration_review", {"days": 5, "text": "CAL x"})
    _, lc = LR.route("live_calibration_review", {"days": 5, "text": "CAL x", "snapshot": {}})
    out.append(_row("info.calibration_review", OPP.CALIBRATE_TASK in tc and OPP.CALIBRATE_TASK in lc,
                    OPP.CALIBRATE_TASK in tc, OPP.CALIBRATE_TASK in lc, "the calibration worker's task, both desks"))
    from guard import rules as GR
    lctx = LR.live_context({"snapshot": {}})
    obj_t = (GR.CHALLENGE["profit_target"], GR.CHALLENGE["min_trading_days"], GR.CHALLENGE["deadline_business_days"])
    said = lambda t: ("+10%" in t and "at least 2" in t and ("NO deadline" in t or "no deadline" in t))
    out.append(_row("info.objective", obj_t == (0.10, 2, None) and said(TR.CONTEXT) and said(lctx)
                    and DS.entry_rules("training") == DS.entry_rules("live"),
                    f"{obj_t}, context says it {said(TR.CONTEXT)}", f"live context says it {said(lctx)}",
                    "the same objective on both desks: +10%, at least 2 traded days, no deadline (and the same checklist)"))
    out.append(_row("info.alert_prompt", DS.ALERT_TASK in ta and DS.ALERT_TASK in la and "SYMBOL FILE" in ta
                    and "SYMBOL FILE" in la, DS.ALERT_TASK in ta, DS.ALERT_TASK in la,
                    "a woken trader gets its symbol file and the same task text"))
    return out


def check_manager() -> list[dict]:
    """The position manager (2026-10-19) wakes on the same bar for the same reasons on both desks: paper.py _manage and
    live_loop.manage_events over one synthetic market -- an in-window trade (R milestones, the plan's INV close, a
    stall, structure against it) and an overnight one (the pre-swap, and a night burst injected into the candles).
    Also: the same task text and the same consult note on both desks."""
    import bisect
    import desk_shared as DS
    import live_broker as LB
    import live_loop as LL
    import paper as P
    import routing as TR
    import live_routing as LR
    from live_fakes import FakeMT5, pos as fpos
    out = []
    end = dt.datetime(2026, 1, 7, 11, 0, tzinfo=dt.timezone.utc)
    m5, _ = _info_market(end)
    burst_t = LB.utc_to_broker_epoch(dt.datetime(2026, 1, 7, 2, 0, tzinfo=dt.timezone.utc))
    for r in m5["XAUUSD"]:
        if r["t"] == burst_t:                       # one night bar about ten times the usual range
            r["h"] = round(r["h"] + 30.0, 2)
    far = LB.utc_to_broker_epoch(end + dt.timedelta(days=30))
    rates = {}
    for sym, rows in m5.items():
        for tf, code in (("M5", "TIMEFRAME_M5"), ("M15", "TIMEFRAME_M15"), ("H1", "TIMEFRAME_H1")):
            src = rows if tf == "M5" else P.resample(rows, tf, far)
            rates[(sym, getattr(FakeMT5, code))] = [{"time": b["t"], "open": b["o"], "high": b["h"], "low": b["l"],
                                                     "close": b["c"]} for b in src]
    tmp = Path(tempfile.mkdtemp(prefix="parity_manage_"))
    for d in ("feed", "state", "live/state"):
        (tmp / d).mkdir(parents=True)
    for sym, rows in m5.items():
        with open(tmp / "feed" / f"{sym}_M5.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t_broker", "o", "h", "l", "c", "session"])
            for r in rows:
                w.writerow([r["t"], r["o"], r["h"], r["l"], r["c"], r["session"]])

    class ClockFake(FakeMT5):
        clock_b = 0

        def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
            rows = [r for r in self._rates.get((symbol, timeframe), []) if r["time"] <= self.clock_b]
            return rows[max(0, len(rows) - count):]

    cases = [("night", "XAUUSD", "buy", dt.datetime(2026, 1, 6, 19, 0, tzinfo=dt.timezone.utc),
              dt.datetime(2026, 1, 7, 3, 0, tzinfo=dt.timezone.utc), 40.0),
             ("day", "EURUSD", "sell", dt.datetime(2026, 1, 7, 8, 0, tzinfo=dt.timezone.utc), end, 0.0012)]
    saved = (LB._now_utc, LB.LEDGER_PATH)
    try:
        for name, sym, side, t_open, t_end, risk in cases:
            p = P.Paper(home=tmp)
            tb_open = LB.utc_to_broker_epoch(t_open)
            entry = next(r["o"] for r in m5[sym] if r["t"] == tb_open)
            buy = side == "buy"
            sl = round(entry - risk if buy else entry + risk, 5)
            inv = round(entry - 0.6 * risk if buy else entry + 0.6 * risk, 5)
            why = f"parity {name} trade, INV close {'<' if buy else '>'} {inv}"
            opened = t_open.strftime("%Y-%m-%d %H:%M")
            p.s["cursor"] = bisect.bisect_right(p.timeline, tb_open - 300)
            p.s["day"] = P.day_key(tb_open)
            for s_, rows in m5.items():
                p.s["last_close"][s_] = next(r["c"] for r in reversed(rows) if r["t"] < tb_open)
            p.s["positions"] = [{"id": 1, "symbol": sym, "side": side, "lots": 0.01, "entry": entry, "sl": sl, "sl0": sl,
                                 "tp": None, "why": why, "risk_usd": 1.0, "risk_usd0": 1.0, "lots0": 0.01,
                                 "realized_usd": 0.0, "opened_utc": opened, "opened_t": tb_open, "day": p.s["day"]}]
            p.s["manage"] = {}
            led = tmp / "state" / "ledger.jsonl"
            n0 = len(led.read_text().splitlines()) if led.exists() else 0
            with contextlib.redirect_stdout(io.StringIO()):
                while p.t_now() is None or p.t_now() < LB.utc_to_broker_epoch(t_end - dt.timedelta(minutes=5)):
                    if not p.s["positions"] or not p._bar():
                        break
                    p._clock(p.t_now())
            rows_t = [json.loads(x) for x in led.read_text().splitlines()[n0:]] if led.exists() else []
            closed_at = next((r["utc"] for r in rows_t if r.get("type") == "trade_closed"), None)
            train = [(r["at_utc"], [x["code"] for x in r["triggers"]]) for r in rows_t if r.get("type") == "manage_wake"]
            fake = ClockFake(rates=rates, positions=[fpos(1, sym, side, 0.01, entry, sl=sl, magic=LB.MAGIC, opened=t_open)])
            LB.LEDGER_PATH = tmp / "live" / "state" / "live_ledger.jsonl"
            LB.LEDGER_PATH.write_text(json.dumps({"type": "order_placed", "ticket": 1, "symbol": sym, "side": side,
                                                  "sl": sl, "why": why, "utc": opened + ":00"}) + "\n")
            (tmp / "live" / "state" / "live_manage.json").unlink(missing_ok=True)
            live = []
            clock = t_open
            while clock < t_end:
                clock += dt.timedelta(minutes=5)
                LB._now_utc = (lambda c: (lambda: c))(clock)
                fake.clock_b = LB.utc_to_broker_epoch(clock)
                snap = {"market_open": True, "own_positions": [{"ticket": 1, "symbol": sym, "side": side, "lots": 0.01,
                                                                "entry": entry, "sl": sl, "tp": None, "floating": 0.0,
                                                                "opened_utc": opened}]}
                live += [(e["at_utc"], [x["code"] for x in e["triggers"]])
                         for e in LL.manage_events(fake, snap, clock, home=tmp / "live")]
            if closed_at:                    # the replay closed it at its stop: compare up to that bar
                train_last = train[-1][0] if train else ""
                live = [x for x in live if x[0] <= max(train_last, closed_at[:16])]
            out.append(_row(f"manage.{name}", train == live and len(train) > 0, train, live,
                            f"{sym} {side}: the manager's wakes, bar by bar, on both desks"))
    finally:
        LB._now_utc, LB.LEDGER_PATH = saved
    pay = {"symbol": "XAUUSD", "side": "buy", "id": 1, "ticket": 1, "entry": 3300.0, "sl": 3290.0, "sl0": 3290.0,
           "lots": 0.1, "opened_utc": "2026-01-07 08:00", "triggers": [{"code": "R+1", "why": "x"}], "info": {},
           "symbol_file": "SYMBOL FILE x", "snapshot": {}}
    tk, tp_ = TR.route("manage_wake", dict(pay))
    lk, lp_ = LR.route("live_manage", dict(pay))
    out.append(_row("info.manage_prompt", DS.MANAGE_TASK in tp_ and DS.MANAGE_TASK in lp_ and "SYMBOL FILE" in tp_
                    and "SYMBOL FILE" in lp_ and "WHY YOU WERE WOKEN" in tp_ and "WHY YOU WERE WOKEN" in lp_,
                    tk, lk, "the manager gets the position block, its symbol file and the same task text"))
    from guard import policy as PO
    t_note = all(DS.consult_note(f"python3 {PO.CONSULT} training") in PO.KINDS[k]["notes"]
                 for k in ("trade", "manage", "risk", *[f"trade_{s.lower().replace('.', '_')}" for s in PO.SYMBOL_TRADERS]))
    l_note = all(DS.consult_note(f"python3 {PO.CONSULT} live") in PO.LIVE_KINDS[k]["notes"]
                 for k in ("live", "live_manage", "live_risk", *PO.LIVE_SYMBOL_TRADER_KINDS))
    out.append(_row("info.consult_note", t_note == l_note == True, t_note, l_note,
                    "every deciding kind is told how to consult chart/timing now, word for word"))
    _, ts = TR.route("turn_swing", {"movers": [], "bars_text": "THE HIGHER TIMEFRAMES x", "snapshot": {}, "goals": []})
    _, ls = LR.route("live_swing", {"movers": [], "bars_text": "THE HIGHER TIMEFRAMES x", "snapshot": {}, "key": "k"})
    out.append(_row("info.swing_prompt", DS.SWING_TASK in ts and DS.SWING_TASK in ls and "THE HIGHER TIMEFRAMES" in ts
                    and "THE HIGHER TIMEFRAMES" in ls, DS.SWING_TASK in ts, DS.SWING_TASK in ls,
                    "the once-a-day swing turn: same task, same higher-timeframe candles (the clock: tests/test_manage)"))
    out.append(_row("info.objective_text", DS.OBJECTIVE in TR.CONTEXT and DS.OBJECTIVE in LR.live_context({"snapshot": {}}),
                    DS.OBJECTIVE in TR.CONTEXT, DS.OBJECTIVE in LR.live_context({"snapshot": {}}),
                    "the most money inside the two hard lines, word for word on both desks"))
    _, tv = TR.route("vault_review", {"day": "d", "vault_text": "THE VAULT x"})
    _, lv = LR.route("live_vault_review", {"day": "d", "vault_text": "THE VAULT x", "snapshot": {}})
    out.append(_row("info.vault_review", DS.VAULT_REVIEW_TASK in tv and DS.VAULT_REVIEW_TASK in lv and "THE VAULT x" in tv
                    and "THE VAULT x" in lv, DS.VAULT_REVIEW_TASK in tv, DS.VAULT_REVIEW_TASK in lv,
                    "the vault's daily value extraction: same paperwork, same task, both desks"))
    _, tr_ = TR.route("risk_review", {"lever": {}})
    _, lr_ = LR.route("live_risk_review", {"lever": {}, "snapshot": {}})
    out.append(_row("info.risk_task", DS.RISK_TASK in tr_ and DS.RISK_TASK in lr_, DS.RISK_TASK in tr_,
                    DS.RISK_TASK in lr_, "the risk checkpoint owns the same levers with the same task, both desks"))
    import live_broker as _LB
    import paper as _P
    out.append(_row("info.own_rails", _P.Paper.LEVERS == _LB.LEVERS, _P.Paper.LEVERS, _LB.LEVERS,
                    "the own rails as risk levers: the same defaults and FTMO-bounded ranges on both desks"))
    out.append(_row("info.widen_rules", DS.WIDEN_RULES in PO.KINDS["risk"]["notes"] and DS.WIDEN_RULES in
                    PO.LIVE_KINDS["live_risk"]["notes"], DS.WIDEN_RULES in PO.KINDS["risk"]["notes"],
                    DS.WIDEN_RULES in PO.LIVE_KINDS["live_risk"]["notes"], "the risk worker owns the widen, both desks"))
    return out


def run_all() -> list[dict]:
    rows = []
    for fn in (check_scan, check_clock, check_routing, check_kinds, check_formats, check_info, check_alerts_and_trader,
               check_manager):
        try:
            rows += fn()
        except Exception as exc:  # a check that cannot run is itself a finding
            rows.append(_row(f"{fn.__name__}.error", False, "-", "-", f"{type(exc).__name__}: {exc}"))
    for r in rows:
        if r["status"] == "DRIFT":
            r["known"] = KNOWN_DRIFT.get(r["check"])
    return rows


def verdict(rows: list[dict]) -> dict:
    new = [r["check"] for r in rows if r["status"] == "DRIFT" and not r.get("known")]
    drifting = {r["check"] for r in rows if r["status"] == "DRIFT"}
    stale = [k for k in KNOWN_DRIFT if k not in drifting]
    return {"new_drift": new, "known_drift_now_same": stale, "ok": not new and not stale}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="parity_check.py")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rows = run_all()
    v = verdict(rows)
    if a.json:
        print(json.dumps({"rows": rows, **v}, default=str, indent=1))
    else:
        for r in rows:
            mark = "  " if r["status"] == "SAME" else ("~ " if r.get("known") else "! ")
            print(f"{mark}{r['status']:5s} {r['check']:32s} training={r['training']!s:40.40s} live={r['live']!s:40.40s}")
            if r["status"] == "DRIFT":
                print(f"        {'known: ' + r['known'] if r.get('known') else 'NEW DRIFT -- ' + r['note']}")
        print(f"\nnew drift: {v['new_drift'] or 'none'}; known drift that is now the same (remove it from "
              f"KNOWN_DRIFT): {v['known_drift_now_same'] or 'none'}")
    return 0 if v["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
