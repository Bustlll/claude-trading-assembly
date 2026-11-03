"""PAPER BROKER (M5 clock): the agent's hands. Paper only. No broker connection exists in this folder.

M5 is the native clock, mirroring the DeltaForce trainer's own 5m cache (offline_trainer_ooslabXspeed/
OOSLAB_DESIGN.md): every other timeframe (M15, H1, H4, D1) is DERIVED by resampling M5 bars, not
downloaded separately. The forming bucket of any derived timeframe is never shown (no repaint).

An ATTEMPT is the live evaluation's terms (guard/rules.py CHALLENGE, since 2026-10-18): reach +10% with at least 2
traded days, never breach 5% daily or 10% total loss, NO deadline. It ends only by passing or breaching; the
account then resets on its own and the next attempt starts on the next bar. Nothing waits for a human: this is
historical data. When the history runs out (an EPOCH), the replay wraps back to its start and plays again, after
firing an epoch_review event for deep self-assessment. The history is still cut into SEGMENTs of 10 business days
(guard/rules.py SEGMENT_BUSINESS_DAYS) -- only where an epoch restarts from and how progress is counted, not a clock
an attempt runs against.

Fill model (conservative, see manual/OPERATOR_MANUAL.md):
  * an order fills at the OPEN of that symbol's next M5 bar, plus half its spread snapshot
  * a stop or target is checked against that symbol's M5 bar high and low (five-minute resolution)
  * a gap through a stop fills at the bar open (worse than the stop)
  * every open position is flattened on the last bar before a weekend gap: no weekend holding
  * commission is charged once per lot at close; P&L uses each symbol's MT5 tick value
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from guard import rules as R  # noqa: E402
import board as BOARD  # noqa: E402  (same dir; checks pre_trade board posts, see place_order)

ROUTED = {"trade_closed", "day_closed", "risk_warning", "rule_breach", "goal_passed",
          "order_rejected", "attempt_ended", "attempt_passed_review", "attempt_breach_review",
          "pass_rate_milestone", "epoch_review", "risk_factor_changed", "setup_candidate", "level_alert",
          "manage_wake", "risk_review"}

# The intra-day scan (see advance()/scan_candidates()): every SCAN_EVERY_BARS M5 bars (~1h), a cheap
# pure-code check -- no Claude call -- looks for a symbol that moved at least SCAN_THRESHOLD_PCT% over
# that window. Only a hit costs anything (one cheap "scout" call); most hourly ticks cost nothing at
# all. This is the tiered 5min-data/hourly-check cadence the operator asked for: hourly screening,
# escalating to a real look only when something actually happened, not a fixed daily cadence regardless.
SCAN_EVERY_BARS = 12            # 12 M5 bars = 1 simulated hour
SCAN_THRESHOLD_PCT = 0.30       # tuned to fire a few times a day, not every hour for every symbol
SCAN_MAX = 5                    # candidates per scan event (live_loop SCAN_MAX)
TF_SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400}  # D1 buckets by Prague day_key instead

# THE DECISION CLOCK -- THE LIVE DESK'S CLOCK (operator, 2026-10-18 afternoon: "training should decide each 5 min
# walk forward and set alerts and everything like live"). The replay stops where live would wake a worker, by the
# SAME rules as tools/live_loop.py tick() (tools/parity_check.py decisions / scouting compare them):
#   * a decision turn every DECISION_EVERY_MIN inside the trading window (07:00, 08:00 ... Madrid), with the top
#     movers of the last hour -- live_loop decision_slot();
#   * between turns, the move screen on EVERY closed M5 bar inside the window: a symbol that moved past the
#     threshold wakes a scout, then is not screened again for REFLAG_AFTER_MIN (live_loop _live_flags);
#   * a price alert whose condition is met inside the window -> its trader;
#   * a risk warning (any hour) -> the decider.
# Before this (morning of 2026-10-18): one decision a day at 09:00 Madrid and each symbol screened once a day,
# a tenth of live's looks -- training practised a different, sparser job than the one live does.
TRAIN_TZ = ZoneInfo(os.environ.get("FTMO_BLANK_TRAIN_WINDOW_TZ", "Europe/Madrid"))


def _hm(text: str) -> int:
    h, m = text.strip().split(":")
    return int(h) * 60 + int(m)


TRAIN_WINDOW_START_MIN, TRAIN_WINDOW_END_MIN = (
    _hm(x) for x in os.environ.get("FTMO_BLANK_TRAIN_WINDOW", "07:00-20:00").split("-", 1))
DECISION_EVERY_MIN = int(os.environ.get("FTMO_BLANK_TRAIN_DECISION_EVERY_MIN", "60"))   # live_loop DECISION_EVERY_MIN
REFLAG_AFTER_MIN = int(os.environ.get("FTMO_BLANK_TRAIN_REFLAG_AFTER_MIN", "60"))       # live_loop REFLAG_AFTER_MIN
DECISION_MOVERS = 8             # live_loop MOVERS_IN_DECISION
import alerts_core as AC  # noqa: E402  (the alert rules, shared with the live desk -- tools/live_alerts.py)
import manage_core as MC  # noqa: E402  (when an open position wakes its manager -- the live loop's rules)
MAX_ACTIVE_ALERTS = AC.MAX_ACTIVE  # price alerts the agent may have waiting at once
ALERT_WHEN = AC.WHEN               # = the vocabulary's T-TOUCH / T-CLOSE / T-HOLD / T-REJECT
ALERT_TF = AC.TFS
# 2026-10-18, first hour of the new build: traders used touch alerts 1-4 points from price as a "wake me in
# 5 minutes" timer (one gold setup woke a trader 5 times in 35 sim minutes, a third of all spend). Their real
# conditions were bar closes ("only if a closed M15 rejects 3230"), so alerts now evaluate those in code, and a
# touch alert closer than this many M15 ATRs to price is refused as a poll.
ALERT_TOUCH_MIN_ATR = AC.TOUCH_MIN_ATR
STOP_EVENTS = ("risk_warning", "rule_breach")  # always worth stopping for, at any hour


def bar_close_local(t: int) -> datetime:
    """Local (TRAIN_TZ) time at which the M5 bar opening at broker time t CLOSES -- the moment its
    numbers are known, so the moment a decision on it can be made."""
    return (bar_utc(t) + timedelta(seconds=300)).astimezone(TRAIN_TZ)


def in_train_window(t: int) -> bool:
    loc = bar_close_local(t)
    mins = loc.hour * 60 + loc.minute
    return loc.weekday() < 5 and TRAIN_WINDOW_START_MIN <= mins < TRAIN_WINDOW_END_MIN


def train_decision_slot(t: int) -> str | None:
    """The decision slot the M5 bar opening at t closes into (live_loop decision_slot on the replay's clock):
    one turn per slot, on the first bar inside it; None outside the window."""
    if not in_train_window(t):
        return None
    loc = bar_close_local(t)
    mins = loc.hour * 60 + loc.minute
    return f"{loc.date().isoformat()}#{(mins - TRAIN_WINDOW_START_MIN) // DECISION_EVERY_MIN}"


def train_next_decision_utc(t: int) -> str | None:
    loc = bar_close_local(t)
    mins = loc.hour * 60 + loc.minute
    nxt = TRAIN_WINDOW_START_MIN + ((mins - TRAIN_WINDOW_START_MIN) // DECISION_EVERY_MIN + 1) * DECISION_EVERY_MIN
    if not in_train_window(t) or nxt >= TRAIN_WINDOW_END_MIN:
        return None
    return loc.replace(hour=nxt // 60, minute=nxt % 60).astimezone(timezone.utc).strftime("%H:%M UTC")


def expire_flags(flags: dict, close_t: int) -> dict:
    """live_loop _live_flags: a flagged symbol is screened again REFLAG_AFTER_MIN after its flag."""
    return {s: at for s, at in flags.items() if close_t - at < REFLAG_AFTER_MIN * 60}


def train_window_text() -> str:
    hm = lambda m: f"{m // 60:02d}:{m % 60:02d}"
    return (f"weekdays {hm(TRAIN_WINDOW_START_MIN)}-{hm(TRAIN_WINDOW_END_MIN)} {TRAIN_TZ.key}, a decision turn every "
            f"{DECISION_EVERY_MIN} min, the move screen on every 5-minute bar in between (a symbol re-screened "
            f"{REFLAG_AFTER_MIN} min after it was flagged) and your price alerts -- the live desk's clock")


def bar_utc(t: int) -> datetime:
    return R.broker_to_utc(t)  # seasonal NY+7h offset, not a fixed +3h -- see guard/rules.py


def day_key(t: int) -> str:
    return bar_utc(t).astimezone(ZoneInfo(R.DAY_TZ)).date().isoformat()


def business_days_between(d0: str, d1: str) -> int:
    """Mon-Fri days from d0 to d1 inclusive (ISO dates). 0 when d1 is before d0."""
    a, b = date.fromisoformat(d0), date.fromisoformat(d1)
    n = 0
    while a <= b:
        if a.weekday() < 5:
            n += 1
        a += timedelta(days=1)
    return n


def fitness_components(closes: list[dict], start_day: str | None, today: str | None, start_balance: float) -> dict:
    """Quality x consistency, measured. score_v0 is a STARTING HYPOTHESIS, not a prescribed trade-off:
    mean net R per trade x share of 5-business-day windows that contain a close, minus the max drawdown fraction."""
    from statistics import median
    bdays = business_days_between(start_day, today) if start_day and today else 0
    rs_price = [c["r_price"] for c in closes if c.get("r_price") is not None]
    rs_net = [c["r_net"] for c in closes if c.get("r_net") is not None]
    wins = sum(1 for c in closes if c.get("result") == "win")
    peak, max_dd = start_balance, 0.0
    for c in closes:
        bal = c.get("balance", start_balance)
        peak = max(peak, bal)
        max_dd = max(max_dd, (peak - bal) / peak if peak else 0.0)
    close_days = sorted({c["closed_utc"][:10] for c in closes if c.get("closed_utc")})
    stall, longest, windows, wins_with_close = 0, 0, 0, 0
    if start_day and today:
        traded = set(close_days)
        days = []
        a, b = date.fromisoformat(start_day), date.fromisoformat(today)
        while a <= b:
            if a.weekday() < 5:
                days.append(a.isoformat())
            a += timedelta(days=1)
        for d in days:
            if d in traded:
                stall = 0
            else:
                stall += 1
                longest = max(longest, stall)
        for i in range(len(days) - 4):
            windows += 1
            if any(x in traded for x in days[i:i + 5]):
                wins_with_close += 1
    consistency = (wins_with_close / windows) if windows else None
    mean_net = sum(rs_net) / len(rs_net) if rs_net else None
    score = (round(mean_net * consistency - max_dd, 4) if mean_net is not None and consistency is not None else None)
    return {
        "closed_trades": len(closes), "business_days_elapsed": bdays,
        "trades_per_business_day": round(len(closes) / bdays, 3) if bdays else None,
        "mean_r_price": round(sum(rs_price) / len(rs_price), 3) if rs_price else None,
        "median_r_price": round(median(rs_price), 3) if rs_price else None,
        "mean_r_net": round(mean_net, 3) if mean_net is not None else None,
        "win_rate": round(wins / len(closes), 3) if closes else None,
        "max_drawdown_pct": round(100 * max_dd, 2), "longest_stall_business_days": longest,
        "window_consistency_5d": round(consistency, 3) if consistency is not None else None,
        "score_v0_hypothesis": score,
        "score_v0_formula": "mean_r_net x window_consistency_5d - max_drawdown_fraction (replace with evidence)",
    }


def load_feed(path: Path) -> list[dict]:
    """Reads the CSV at `path`, or its binary cache (same stem, .npy) when that exists and is at least
    as new as the CSV. build_specs.py writes the cache; a test fixture that only writes a CSV falls
    back to parsing it, so this is transparent either way."""
    cache = path.with_suffix(".npy")
    if cache.exists() and cache.stat().st_mtime >= path.stat().st_mtime:
        import numpy as np
        arr = np.load(cache)
        # whole-column .tolist() is one vectorised pass; converting each numpy scalar one row at a
        # time (the obvious way) is the actual bottleneck here -- it dominated load time end to end
        cols = (arr["t"].tolist(), arr["o"].tolist(), arr["h"].tolist(), arr["l"].tolist(),
                arr["c"].tolist(), arr["session"].tolist())
        return [{"t": t, "o": o, "h": h, "l": l, "c": c, "session": ss} for t, o, h, l, c, ss in zip(*cols)]
    with open(path) as f:
        return [{"t": int(r["t_broker"]), "o": float(r["o"]), "h": float(r["h"]), "l": float(r["l"]),
                 "c": float(r["c"]), "session": r.get("session", "")} for r in csv.DictReader(f)]


def resample(bars: list[dict], tf: str, upto_t: int | None, forming_at: int | None = None) -> list[dict]:
    """Group M5 bars into tf buckets, dropping the one bucket that is still forming (it may not have all its
    M5 bars yet: showing it would repaint). bars must be sorted by t; only bars with t <= upto_t are used.
    The forming bucket is the one forming_at falls inside, default upto_t. The replay passes
    forming_at = t_now + 300, the moment its last bar CLOSED (2026-10-18, found by tools/parity_check.py
    info.scout_candles): with upto_t = that bar's OPEN time, the M15/H1/H4 bucket the bar completes was
    treated as forming, so at every top-of-the-hour stop -- every scan, every 09:00 decision turn -- the
    workers saw the last closed M15 and H1 bar missing, one bar behind what the live desk's broker shows."""
    if tf == "M5":
        return [b for b in bars if upto_t is None or b["t"] <= upto_t]

    def key_of(t: int):
        return day_key(t) if tf == "D1" else t // TF_SECONDS[tf]

    buckets: dict = {}
    for b in bars:
        if upto_t is not None and b["t"] > upto_t:
            break
        buckets.setdefault(key_of(b["t"]), []).append(b)
    at = forming_at if forming_at is not None else upto_t
    forming = key_of(at) if at is not None else None
    keys = sorted((k for k in buckets if k != forming), key=lambda k: buckets[k][0]["t"])
    out = []
    for k in keys:
        grp = buckets[k]
        out.append({"t": grp[0]["t"], "o": grp[0]["o"], "h": max(x["h"] for x in grp),
                    "l": min(x["l"] for x in grp), "c": grp[-1]["c"], "session": grp[-1]["session"]})
    return out


MIN_FIRST_TARGET_R = 1.5


def rr_refusal(side: str, entry: float, sl, tp) -> str | None:
    """Training's order gate (2026-11-01, measured on the vault's rows, not lesson text): planned RR under 1.5 lost on the
    desk's own trades (n=13, -0.61R) and across all runners' 1-1.5 band (n=55, -0.27R); 1.5 and up did not. Not a filter on how
    often it trades: a target at real structure further away passes."""
    if tp is None:
        return "a first target is required on the training desk: --tp at real structure, at least 1.5R from the entry"
    stop, gain = abs(entry - float(sl)), abs(float(tp) - entry)
    if stop > 0 and gain / stop < MIN_FIRST_TARGET_R:
        return (f"first target pays {gain / stop:.2f}R from about {entry:.5g}; the desk's minimum is {MIN_FIRST_TARGET_R}R "
                "(planned RR under 1.5 has lost on its record). Pick a real level further away, or plan the entry at a better price.")
    return None


class Paper:
    def __init__(self, home: Path = HOME):
        self.home = Path(home)
        self.deadline = R.CHALLENGE["deadline_business_days"]   # fixed for this replay's life (None = no deadline)
        self.feeds: dict[str, list[dict]] = {}
        self.by_t: dict[str, dict[int, dict]] = {}
        for sym in R.INSTRUMENTS:
            path = self.home / "feed" / f"{sym}_M5.csv"
            if path.exists():
                bars = load_feed(path)
                self.feeds[sym] = bars
                self.by_t[sym] = {b["t"]: b for b in bars}
        if not self.feeds:
            raise SystemExit(f"no feeds in {self.home / 'feed'}; run tools/build_specs.py")
        counts: dict[int, int] = {}
        for bars in self.feeds.values():
            for b in bars:
                counts[b["t"]] = counts.get(b["t"], 0) + 1
        need = min(R.REPLAY_MIN_SYMBOLS, len(self.feeds))
        start = min(t for t, c in counts.items() if c >= need)
        self.timeline = sorted(t for t in counts if t >= start)
        self.windows = self._compute_windows()
        self.state_path = self.home / "state" / "account.json"
        self.ledger_path = self.home / "state" / "ledger.jsonl"
        self.outbox_path = self.home / "state" / "outbox.jsonl"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if self.state_path.exists():
            self.s = json.loads(self.state_path.read_text())
        else:
            self.s = self._fresh_state()
            if self.windows:
                self.s["cursor"] = self._index_of(self.windows[0][0])
        self._jump_cursor: int | None = None
        self._stop_reason: str | None = None  # set by an event worth stopping the replay for (advance_to_decision)

    def _fresh_state(self) -> dict:
        return {"cursor": 0, "balance": R.START_BALANCE, "equity": R.START_BALANCE, "day": None,
                "day_start_equity": R.START_BALANCE, "traded_days": [], "positions": [], "pending": [],
                "next_id": 1, "breached": None, "field_read": False, "notes_count": 0, "note_days": [],
                "consec_losses": 0, "warned": [], "goals_passed": [], "last_close": {}, "attempt": 1,
                "epoch": 0, "window_index": 0, "attempts_total": 0, "attempts_passed": 0,
                "milestones_fired": [], "start_day": None,
                # the "risk" kind's own lever, bounded by guard/rules.py (RISK_FACTOR_MIN..MAX_RISK_PER_TRADE);
                # intentionally NOT reset by _end_window -- it is meant to move over the account's whole
                # life, not snap back to default every two weeks.
                "risk_pct": R.RISK_FACTOR_DEFAULT, "risk_factor_history": [],
                # 2026-10-20: the own rails as levers (live_broker LEVERS twin), persistent like risk_pct
                "risk_levers": {}, "peak_equity": R.START_BALANCE, "risk_checked_day": None,
                "scan_flags": {},          # symbol -> broker-epoch close it was flagged at (expire_flags); cleared in _end_day()
                "decided_slot": None,      # the last decision slot a turn fired in (train_decision_slot)
                "alerts": [], "next_alert_id": 1,  # price alerts (paper.py alert), see _check_alerts
                "consecutive_passes": 0, "best_pass_streak": 0, "risk_warnings_this_attempt": 0}

    def _index_of(self, t: int) -> int:
        import bisect
        return bisect.bisect_left(self.timeline, t)

    def _compute_windows(self) -> list[tuple[int, int]]:
        """Partition the usable M5 history into back-to-back 10-business-day windows (Mon-Fri only)."""
        days_seen: dict[str, int] = {}  # day_key -> first timeline index of that day
        for i, t in enumerate(self.timeline):
            dk = day_key(t)
            if dk not in days_seen:
                days_seen[dk] = i
        business_days = [dk for dk in days_seen if date.fromisoformat(dk).weekday() < 5]
        business_days.sort(key=lambda dk: days_seen[dk])
        n = self.deadline or R.SEGMENT_BUSINESS_DAYS
        windows = []
        for i in range(0, len(business_days) - n + 1, n):
            start_t = self.timeline[days_seen[business_days[i]]]
            if i + n < len(business_days):
                end_t = self.timeline[days_seen[business_days[i + n]]]
            else:
                end_t = self.timeline[-1] + 1
            windows.append((start_t, end_t))
        return windows

    # ---- persistence -----------------------------------------------------
    def save(self):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, indent=1))
        os.replace(tmp, self.state_path)

    def _append(self, path: Path, rec: dict):
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def log(self, typ: str, **data):
        rec = {"type": typ, "bar": self.s["cursor"], "utc": self.cur_time(), **data}
        self._append(self.ledger_path, rec)
        if typ in ROUTED:
            self._append(self.outbox_path, rec)
        if typ in STOP_EVENTS:
            self._stop_reason = self._stop_reason or typ
        return rec

    # ---- clock and prices -------------------------------------------------
    def t_now(self):
        c = self.s["cursor"]
        return self.timeline[c - 1] if c else None

    def cur_time(self) -> str:
        t = self.t_now()
        return bar_utc(t).strftime("%Y-%m-%d %H:%M UTC") if t else "not started"

    def half_spread(self, sym: str) -> float:
        return R.spec(sym)["spread_price_snapshot"] / 2

    def mark(self, p: dict) -> float:
        return self.s["last_close"].get(p["symbol"], p["entry"])

    def exit_mark(self, p: dict, level: float) -> float:
        h = self.half_spread(p["symbol"])
        return level - h if p["side"] == "buy" else level + h

    def pnl_usd(self, p: dict, price: float) -> float:
        d = (price - p["entry"]) if p["side"] == "buy" else (p["entry"] - price)
        return d * p["lots"] * R.spec(p["symbol"])["usd_per_price_unit_per_lot"]

    def equity_now(self) -> float:
        return self.s["balance"] + sum(self.pnl_usd(p, self.exit_mark(p, self.mark(p))) for p in self.s["positions"])

    def open_risk(self) -> float:
        return sum(R.risk_usd(p["symbol"], p["lots"], p["entry"], p["sl"]) for p in self.s["positions"]
                   if p.get("sl") is not None)

    def pending_risk(self) -> float:
        return sum(R.risk_usd(o["symbol"], o["lots"], self.s["last_close"][o["symbol"]], o["sl"])
                   for o in self.s["pending"] if o["kind"] == "open" and o["symbol"] in self.s["last_close"])

    # ---- agent-facing reads ----------------------------------------------
    def _segment_index(self) -> int:
        """Which history segment the replay is in (progress through the data; not a deadline since 2026-10-18)."""
        t = self.t_now()
        if not self.windows or t is None:
            return self.s.get("window_index", 0)
        return max(0, bisect.bisect_right([w[0] for w in self.windows], t) - 1)

    def window_day(self) -> int:
        if not self.s.get("start_day") or not self.s["day"]:
            return 0
        return business_days_between(self.s["start_day"], self.s["day"])

    def floating_positions(self) -> list[dict]:
        """Open positions with their current floating P&L and mark price, not just entry/sl/tp."""
        out = []
        for p in self.s["positions"]:
            mark = self.exit_mark(p, self.mark(p))
            out.append({**p, "mark_price": round(mark, 6), "floating_usd": round(self.pnl_usd(p, mark), 2)})
        return out

    def _ledger_tail(self, max_lines: int = 20_000) -> list[dict]:
        """A bounded read from the end of the ledger: enough to cover the current attempt (at most a
        few hundred trades over 10 business days) and recent attempt_ended records, without the cost
        of scanning a ledger that keeps growing over a long-running service."""
        if not self.ledger_path.exists():
            return []
        with open(self.ledger_path) as f:
            lines = f.readlines()[-max_lines:]
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def attempt_trades(self) -> list[dict]:
        """Every trade_closed row for the CURRENT attempt."""
        current = self.s.get("attempt", 1)
        return [r for r in self._ledger_tail() if r.get("type") == "trade_closed" and r.get("attempt", 1) == current]

    def trades_by_day_this_attempt(self) -> dict:
        by_day: dict[str, int] = {}
        for t in self.attempt_trades():
            d = (t.get("closed_utc") or "")[:10]
            by_day[d] = by_day.get(d, 0) + 1
        return dict(sorted(by_day.items()))

    def recent_attempts(self, n: int = 8) -> list[dict]:
        """The last n attempt_ended records, each carrying a freshly recomputed demo_passed flag
        (see guard/rules.py demo_passed) -- the stored outcome string can be stale if the profit
        target has changed since that attempt actually ended, which it has (10% -> 5%)."""
        rows = [r for r in self._ledger_tail() if r.get("type") == "attempt_ended"]
        for r in rows:
            r["demo_passed_now"] = R.demo_passed(r.get("final_equity", 0), r.get("traded_days", 0))
        return rows[-n:]

    def real_eval_chances(self) -> dict:
        """Every attempt that clears TODAY's demo bar earns one chance at the real two-step FTMO
        evaluation this demo is practice for (field/real_progression.md) -- recomputed fresh from
        the ledger every time, not a stored counter, so it is never stale if the target changes
        again. None spent yet: there is no real-eval track built to spend them on."""
        rows = [r for r in self._ledger_tail() if r.get("type") == "attempt_ended"
                and R.demo_passed(r.get("final_equity", 0), r.get("traded_days", 0))]
        return {"earned": len(rows), "used": 0, "available": len(rows),
                "passes": [{"attempt": r["attempt"], "final_equity": r["final_equity"],
                           "end_day": r.get("end_day")} for r in rows]}

    def snapshot(self) -> dict:
        c = self.s["cursor"]
        eq = self.s["equity"]
        day_used = self.s["day_start_equity"] - eq
        total_used = R.START_BALANCE - eq
        rate = (self.s["attempts_passed"] / self.s["attempts_total"]) if self.s["attempts_total"] else None
        positions = self.floating_positions()
        today = self.s.get("day")
        attempt_trades = self.attempt_trades()
        trades_today = [t for t in attempt_trades if (t.get("closed_utc") or "")[:10] == today]
        return {
            "bar_index": c, "bars_total": len(self.timeline), "utc": self.cur_time(),
            # 2026-11-01: the moment the desk is deciding at -- the CLOSE of the last M5 bar (utc is that bar's open,
            # 5 minutes earlier); live's snapshot utc is the wall clock just after that same close
            "now_utc": (bar_utc(self.t_now() + 300).strftime("%Y-%m-%d %H:%M UTC") if self.t_now() else None),
            "prague_day": self.s["day"], "symbols_replayable": len(self.feeds),
            "last_close": dict(self.s["last_close"]),
            "balance": round(self.s["balance"], 2), "equity": round(eq, 2),
            "day_start_equity": round(self.s["day_start_equity"], 2),
            "day_pnl": round(eq - self.s["day_start_equity"], 2),
            "daily_loss_used_usd": round(day_used, 2), "daily_loss_limit_usd": round(R.daily_loss_limit_usd(), 2),
            "total_loss_used_usd": round(total_used, 2), "total_loss_limit_usd": round(R.total_loss_limit_usd(), 2),
            "open_risk_usd": round(self.open_risk(), 2),
            "floating_pnl_usd": round(sum(p["floating_usd"] for p in positions), 2),
            "positions": positions, "pending": [dict(o) for o in self.s["pending"]],
            "traded_days": len(self.s["traded_days"]), "rungs_passed": R.rungs_passed(eq),
            "breached": self.s["breached"],
            "attempt": self.s["attempt"], "epoch": self.s["epoch"], "window_index": self._segment_index(),
            "windows_per_epoch": len(self.windows),
            "window_day": self.window_day(), "window_business_days": R.CHALLENGE["deadline_business_days"],
            "attempt_day": self.window_day(), "profit_target_pct": round(100 * R.CHALLENGE["profit_target"], 2),
            "min_trading_days": R.CHALLENGE["min_trading_days"],
            "attempts_total": self.s["attempts_total"], "attempts_passed": self.s["attempts_passed"],
            "pass_rate": round(rate, 4) if rate is not None else None,
            "trades_closed_today": len(trades_today),
            "trades_closed_this_attempt": len(attempt_trades),
            "trades_by_day_this_attempt": self.trades_by_day_this_attempt(),
            "risk_pct": self.s.get("risk_pct", R.RISK_FACTOR_DEFAULT),
            "per_trade_cap_usd": round(R.effective_risk_pct(self.s) * R.START_BALANCE, 2),
            "consecutive_passes": self.s.get("consecutive_passes", 0),
            "best_pass_streak": self.s.get("best_pass_streak", 0),
            "real_eval_chances": self.real_eval_chances()["earned"],
            "alerts_waiting": [{k: a[k] for k in ("id", "symbol", "dir", "level", "why", "by", "expires_utc")}
                               for a in self.s.get("alerts") or []],
            "trading_window": train_window_text(),
        }

    def goals(self) -> list[dict]:
        return R.goal_table(self._goal_state())

    def _goal_state(self) -> dict:
        return {"equity": self.s["equity"], "traded_days": self.s["traded_days"],
                "note_days": self.s.get("note_days", []), "field_read": self.s["field_read"],
                "breached": self.s["breached"], "window_day": self.window_day(),
                "risk_pct": self.s.get("risk_pct", R.RISK_FACTOR_DEFAULT)}

    def symbols(self) -> list[dict]:
        out = []
        for sym in sorted(self.feeds):
            sp = R.spec(sym)
            out.append({"symbol": sym, "class": sp["class"], "last_close": self.s["last_close"].get(sym),
                        "volume_min": sp["volume_min"], "volume_step": sp["volume_step"],
                        "usd_per_price_unit_per_lot": sp["usd_per_price_unit_per_lot"],
                        "spread_price_snapshot": sp["spread_price_snapshot"],
                        "commission_rt_per_lot": sp["commission_rt_per_lot"]})
        return out

    def bars_view(self, symbol: str, n: int, tf: str = "M5", session: str | None = None) -> list[dict]:
        if symbol not in self.feeds:
            return [{"error": f"no replay feed for {symbol}"}]
        t_now = self.t_now()
        if t_now is None:
            return []
        rows = resample(self.feeds[symbol], tf, t_now, forming_at=t_now + 300)
        if session in ("day", "night") and tf == "M5":
            rows = [b for b in rows if b["session"] == session]
        out = []
        for b in rows[-n:]:
            out.append({"utc": bar_utc(b["t"]).strftime("%Y-%m-%d %H:%M"), "day": day_key(b["t"]),
                        "tf": tf, "session": b.get("session", ""), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]})
        return out

    def recent_bars(self, symbol: str, n: int, tf: str = "M15") -> list[dict]:
        """bars_view() for the last few bars only, without resampling the symbol's whole history (the
        driver attaches these to a scout's or trader's prompt, so it does not spend a tool call on them)."""
        if symbol not in self.feeds or self.t_now() is None:
            return []
        per = 288 if tf == "D1" else TF_SECONDS.get(tf, 300) // 300   # D1 buckets by Prague day: ~288 M5 bars
        i = bisect.bisect_right(self._symbol_ts(symbol), self.t_now())
        chunk = self.feeds[symbol][max(0, i - per * (n + 3)):i]
        rows = resample(chunk, tf, self.t_now(), forming_at=self.t_now() + 300)
        if tf != "M5" and rows:
            rows = rows[1:]  # the first bucket of the slice may be cut short
        return [{"utc": bar_utc(b["t"]).strftime("%Y-%m-%d %H:%M"), "day": day_key(b["t"]), "tf": tf,
                 "session": b.get("session", ""), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
                for b in rows[-n:]]

    # ---- agent-facing writes ---------------------------------------------
    def mark_field_read(self):
        self.s["field_read"] = True
        self.log("field_read")
        self.save()

    def _consulted_analysis(self, symbol: str) -> bool:
        """True if a specialist posted a pre_trade finding for this symbol on the board, today,
        before this order -- checked by the broker itself, not self-reported by whoever is placing
        the order. This is what lets a later review tell a trade taken WITH prior analysis apart
        from one taken WITHOUT it, without trusting the trader's own claim."""
        try:   # a specialist's consult answer (tools/consult.py, 2026-10-19) counts as prior analysis too
            posts = (BOARD.read(topic="pre_trade", symbol=symbol, n=20, home=self.home)
                     + BOARD.read(topic="consult", symbol=symbol, n=20, home=self.home))
        except Exception:
            return False
        return any(p.get("utc", "")[:10] == self.s.get("day") for p in posts)

    def _castigated(self) -> bool:
        """True while tools/castigation.py has the desk castigated: the order is tagged, so a review and the vault
        can tell a trade the desk chose from one the burn forced."""
        try:
            return json.loads((self.home / "state" / "castigation.json").read_text()).get("state") == "castigated"
        except (OSError, ValueError):
            return False

    def place_order(self, symbol: str, side: str, lots: float, sl, tp, why: str, rr_gate: bool = False) -> dict:
        if self.t_now() is None:
            return {"ok": False, "reason": "no bars yet; the replay has not started"}
        if len(why.strip()) < 10:
            return {"ok": False, "reason": "a rationale is required (at least 10 characters) in --why"}
        if symbol not in self.feeds:
            return {"ok": False, "reason": f"{symbol} is not replayable. Replayable: {sorted(self.feeds)}"}
        ref = self.s["last_close"].get(symbol)
        if ref is None:
            return {"ok": False, "reason": f"no price for {symbol} yet; it has not had a bar"}
        block = self._own_rails_block()
        if block:
            self.log("order_refused", symbol=symbol, side=side, reason=block)   # 2026-11-01: a refusal was invisible in the ledger
            return {"ok": False, "reason": block}
        if rr_gate:
            est0 = ref + self.half_spread(symbol) if side == "buy" else ref - self.half_spread(symbol)
            why_rr = rr_refusal(side, est0, sl, tp)
            if why_rr:
                self.log("order_refused", symbol=symbol, side=side, reason=why_rr)
                return {"ok": False, "reason": why_rr}
        state = self._goal_state() | {"equity": self.s["equity"], "day_start_equity": self.s["day_start_equity"],
                                      "breached": self.s["breached"]}
        est = ref + self.half_spread(symbol) if side == "buy" else ref - self.half_spread(symbol)
        ok, reason = R.order_check(state, {"symbol": symbol, "side": side, "lots": lots, "sl": sl, "tp": tp},
                                   est, self.open_risk(), self.pending_risk())
        if not ok:
            return {"ok": False, "reason": reason}
        oid = self.s["next_id"]
        self.s["next_id"] += 1
        consulted = self._consulted_analysis(symbol)
        forced = self._castigated()
        self.s["pending"].append({"id": oid, "kind": "open", "symbol": symbol, "side": side, "lots": lots,
                                  "sl": sl, "tp": tp, "why": why.strip(), "ref": ref, "placed_bar": self.s["cursor"],
                                  "consulted_analysis": consulted})
        self.log("order_placed", id=oid, symbol=symbol, side=side, lots=lots, sl=sl, tp=tp, why=why.strip(),
                 ref=ref, check=reason, consulted_analysis=consulted, **({"castigated": True} if forced else {}))
        self.save()
        return {"ok": True, "id": oid, "symbol": symbol, "fills_at": f"next {symbol} bar open", "check": reason,
                "consulted_analysis": consulted}

    def close_position(self, pid: int, why: str, symbol: str | None = None, volume: float | None = None) -> dict:
        if len(why.strip()) < 10:
            return {"ok": False, "reason": "a reason is required in --why"}
        p = next((x for x in self.s["positions"] if x["id"] == pid), None)
        if p is None:
            return {"ok": False, "reason": f"no open position {pid}"}
        if symbol is not None and p["symbol"].upper() != symbol.upper():
            return {"ok": False, "reason": f"position {pid} belongs to {p['symbol']}, not {symbol}"}
        vol, partial = None, False
        if volume is not None:     # the live desk's partial close (live_broker.close_position), same rounding
            sp = R.spec(p["symbol"])
            vmin, vstep = sp["volume_min"], sp["volume_step"]
            v = round(math.floor(round(float(volume) / vstep, 6)) * vstep, 8)
            if v <= 0:
                return {"ok": False, "reason": f"--volume {volume} rounds to 0 (step {vstep})"}
            if v < p["lots"] - 1e-9:
                rest = round(p["lots"] - v, 8)
                if v < vmin - 1e-12 or rest < vmin - 1e-12:
                    return {"ok": False, "reason": (f"cannot split {p['lots']} lots into {v} closed + {rest} left: both "
                                                    f"parts must be >= the minimum {vmin} (step {vstep}). Close all of "
                                                    "it, or move the stop instead")}
                vol, partial = v, True
        self.s["pending"].append({"id": pid, "kind": "close", "symbol": p["symbol"], "why": why.strip(),
                                  "placed_bar": self.s["cursor"], **({"volume": vol} if partial else {})})
        self.log("close_placed", id=pid, why=why.strip(), **({"volume": vol, "partial": True} if partial else {}))
        self.save()
        return {"ok": True, "fills_at": "next bar open of that symbol", "partial": partial,
                **({"closed_lots": vol, "remaining_lots": round(p["lots"] - vol, 8)} if partial else {})}

    def modify_position(self, pid: int, new_sl, new_tp, why: str, symbol: str | None = None) -> dict:
        """The live desk's modify (live_broker.modify_position): the stop toward profit only, the target either
        way, both on their own side of the last close."""
        if len(why.strip()) < 10:
            return {"ok": False, "reason": "a reason is required in --why"}
        if new_sl is None and new_tp is None:
            return {"ok": False, "reason": "give --sl and/or --tp"}
        p = next((x for x in self.s["positions"] if x["id"] == pid), None)
        if p is None:
            return {"ok": False, "reason": f"no open position {pid}"}
        if symbol is not None and p["symbol"].upper() != symbol.upper():
            return {"ok": False, "reason": f"position {pid} belongs to {p['symbol']}, not {symbol}"}
        ref = self.s["last_close"].get(p["symbol"])
        buy = p["side"] == "buy"
        if new_sl is not None:
            ok, reason = R.modify_check(p["side"], p["sl"], new_sl)
            if not ok:
                return {"ok": False, "reason": reason + (" (the risk worker alone may widen a stop: paper.py widen)"
                                                         if "widen" in reason else "")}
            if ref is not None and ((buy and new_sl >= ref) or (not buy and new_sl <= ref)):
                return {"ok": False, "reason": f"new stop {new_sl} is on the wrong side of {ref}: close instead"}
        if new_tp is not None:
            if new_tp <= 0:
                return {"ok": False, "reason": "a target must be a price (> 0)"}
            if ref is not None and ((buy and new_tp <= ref) or (not buy and new_tp >= ref)):
                return {"ok": False, "reason": f"target {new_tp} is already behind the current price {ref}: close instead"}
        old_sl, old_tp = p["sl"], p.get("tp")
        if new_sl is not None:
            p["sl"] = new_sl
            self.log("stop_moved", id=pid, symbol=p["symbol"], old_sl=old_sl, new_sl=new_sl, why=why.strip())
        if new_tp is not None:
            p["tp"] = new_tp
            self.log("target_moved", id=pid, symbol=p["symbol"], old_tp=old_tp, new_tp=new_tp, why=why.strip())
        self.save()
        return {"ok": True, "old_sl": old_sl, "new_sl": p["sl"], "old_tp": old_tp, "new_tp": p.get("tp")}

    def modify_stop(self, pid: int, new_sl: float, why: str, symbol: str | None = None) -> dict:
        return self.modify_position(pid, new_sl, None, why, symbol)

    WIDEN_MAX_RISK_MULT = 2.0   # live_broker.WIDEN_MAX_RISK_MULT

    def widen_stop(self, pid: int, new_sl: float, why: str, symbol: str | None = None) -> dict:
        """The live desk's widen (live_broker.widen_stop): the risk kind only, once per trade, at most twice the
        trade's risk at entry, and only if every open stop could still hit inside the daily and total limits."""
        if len(why.strip()) < 20:
            return {"ok": False, "reason": "a widen needs its reason in --why (the level it must clear and why, >= 20 chars)"}
        p = next((x for x in self.s["positions"] if x["id"] == pid), None)
        if p is None:
            return {"ok": False, "reason": f"no open position {pid}"}
        if symbol is not None and p["symbol"].upper() != symbol.upper():
            return {"ok": False, "reason": f"position {pid} belongs to {p['symbol']}, not {symbol}"}
        if p.get("widened"):
            return {"ok": False, "reason": "this position's stop was already widened once"}
        buy = p["side"] == "buy"
        if (buy and not new_sl < p["sl"]) or (not buy and not new_sl > p["sl"]):
            return {"ok": False, "reason": f"{new_sl} is not wider than the current stop {p['sl']}: that is a modify, not a widen"}
        ref = self.s["last_close"].get(p["symbol"])
        if ref is not None and ((buy and new_sl >= ref) or (not buy and new_sl <= ref)):
            return {"ok": False, "reason": f"stop {new_sl} is on the wrong side of {ref}"}
        risk0 = p.get("risk_usd0") or p["risk_usd"]
        risk_new = R.risk_usd(p["symbol"], p["lots"], p["entry"], new_sl)
        if risk_new > self.WIDEN_MAX_RISK_MULT * risk0 + 1e-9:
            return {"ok": False, "reason": (f"the new stop risks ${risk_new:.2f} from entry, more than "
                                            f"{self.WIDEN_MAX_RISK_MULT:g}x the trade's own risk at entry (${risk0:.2f}); "
                                            "a stop that far is a different trade")}
        open_after = sum(R.risk_usd(q["symbol"], q["lots"], q["entry"], new_sl if q["id"] == pid else q["sl"])
                         for q in self.s["positions"] if q.get("sl") is not None)
        eq = self.s["equity"]
        worst_day = self.s["day_start_equity"] - eq + open_after + self.pending_risk()
        worst_total = R.START_BALANCE - eq + open_after + self.pending_risk()
        if (worst_day >= R.START_BALANCE * R.CHALLENGE["max_daily_loss"]
                or worst_total >= R.START_BALANCE * R.CHALLENGE["max_total_loss"]):
            return {"ok": False, "reason": (f"with this stop, every open stop hitting would cost the day ${worst_day:.2f} "
                                            f"/ the account ${worst_total:.2f}: at or past the limit")}
        old = p["sl"]
        p["sl"] = new_sl
        p["widened"] = True
        self.log("stop_widened", id=pid, symbol=p["symbol"], old_sl=old, new_sl=new_sl, risk_at_entry=round(risk0, 2),
                 risk_new=round(risk_new, 2), why=why.strip())
        self.save()
        return {"ok": True, "old_sl": old, "new_sl": new_sl, "risk_at_entry": round(risk0, 2), "risk_new": round(risk_new, 2)}

    def set_watch(self, pid: int, symbol: str | None, above=None, below=None, in_min=None, quiet=False,
                  quiet_min=None, clear=False, why: str = "") -> dict:
        """The manager's standing watch on one position (tools/manage_core.py set_watch; the live twin is
        live_market.py watch): re-wake at a level or a time, or QUIET -- let it rip."""
        p = next((x for x in self.s["positions"] if x["id"] == pid), None)
        if p is None:
            return {"ok": False, "reason": f"no open position {pid}"}
        if symbol is not None and p["symbol"].upper() != symbol.upper():
            return {"ok": False, "reason": f"position {pid} belongs to {p['symbol']}, not {symbol}"}
        t = self.t_now()
        if t is None:
            return {"ok": False, "reason": "no bars yet"}
        now = bar_utc(t + 300)
        st_all = self.s.setdefault("manage", {})
        st, rep = MC.set_watch(st_all.get(str(pid)), now, side=p["side"], price=self.s["last_close"].get(p["symbol"]),
                               above=above, below=below, in_min=in_min, quiet=quiet, quiet_min=quiet_min, clear=clear,
                               why=why)
        if rep["ok"]:
            st_all[str(pid)] = st
            self.log("manage_watch", id=pid, symbol=p["symbol"], watch=rep.get("watch"), why=(why or "").strip())
            self.save()
        return rep

    # ---- the own rails as levers (2026-10-20; the live twin is live_broker LEVERS / set_risk_levers) ----
    LEVERS = {"day_halt_pct": (R.OWN_DAY_HALT_DEFAULT, 0.001, R.OWN_DAY_HALT_MAX),
              "giveback_halt_pct": (R.OWN_GIVEBACK_DEFAULT, 0.001, R.OWN_GIVEBACK_MAX),
              "max_positions": (R.MAX_POSITIONS_DEFAULT, 1, R.MAX_POSITIONS_CAP)}

    def lever(self, name: str):
        default, lo, hi = self.LEVERS[name]
        try:
            v = float((self.s.get("risk_levers") or {}).get(name, default))
        except (TypeError, ValueError):
            v = default
        v = max(lo, min(hi, v))
        return int(v) if name == "max_positions" else v

    def _own_rails_block(self) -> str | None:
        """The desk's own rails on its own P&L (live_broker._safety_halt_block's twin), each a lever of the risk kind."""
        anchor = R.START_BALANCE
        eq = self.equity_now()
        day = self.s["day_start_equity"] - eq
        if day > 0 and day >= self.lever("day_halt_pct") * anchor - 1e-9:
            return (f"day-loss halt: the day is down ${day:.2f}, past the {self.lever('day_halt_pct') * 100:.1f}% halt "
                    f"(a risk lever)")
        gb = float(self.s.get("peak_equity") or anchor) - eq
        if gb >= self.lever("giveback_halt_pct") * anchor - 1e-9:
            return (f"peak-giveback halt: ${gb:.2f} given back from today's peak "
                    f"(>= {self.lever('giveback_halt_pct') * 100:.1f}%, a risk lever)")
        n = len(self.s["positions"]) + sum(1 for o in self.s["pending"] if o["kind"] == "open")
        if n >= self.lever("max_positions"):
            return f"max concurrent positions: already {n} open or pending (limit {self.lever('max_positions')}, a risk lever)"
        return None

    def set_risk_levers(self, why: str, day_halt=None, giveback=None, max_positions=None) -> dict:
        if len((why or "").strip()) < 10:
            return {"ok": False, "reason": "a reason is required (at least 10 characters) in --why"}
        want = {k: v for k, v in (("day_halt_pct", day_halt), ("giveback_halt_pct", giveback),
                                  ("max_positions", max_positions)) if v is not None}
        if not want:
            return {"ok": False, "reason": "give --day-halt, --giveback and/or --max-positions"}
        for k, v in want.items():
            _, lo, hi = self.LEVERS[k]
            if not (lo - 1e-12 <= float(v) <= hi + 1e-12):
                return {"ok": False, "reason": f"{k} {v} is outside [{lo}, {hi}] (bounded by FTMO's hard lines)"}
        entry = {"utc": self.cur_time(), "why": why.strip(), **{k: {"old": self.lever(k), "new": v} for k, v in want.items()}}
        lv = self.s.setdefault("risk_levers", {})
        for k, v in want.items():
            lv[k] = int(v) if k == "max_positions" else float(v)
        self.s.setdefault("risk_factor_history", []).append(entry)
        self.log("risk_levers_changed", **entry)
        self.save()
        return {"ok": True, **{k: self.lever(k) for k in want}}

    def risk_factor(self) -> dict:
        """The current per-trade risk lever and the own rails, and the ranges they are clamped to -- read-only, no
        rationale required, so anyone can check it before deciding whether to propose a change."""
        current = self.s.get("risk_pct", R.RISK_FACTOR_DEFAULT)
        return {"risk_pct": current, "effective_pct": R.effective_risk_pct(self.s),
                "per_trade_cap_usd": round(R.effective_risk_pct(self.s) * R.START_BALANCE, 2),
                "min": R.RISK_FACTOR_MIN, "max": R.MAX_RISK_PER_TRADE,
                "levers": {k: {"value": self.lever(k), "min": lo, "max": hi} for k, (_, lo, hi) in self.LEVERS.items()},
                "hard_lines": {"daily_loss": R.CHALLENGE["max_daily_loss"], "total_loss": R.CHALLENGE["max_total_loss"]},
                "history": self.s.get("risk_factor_history", [])[-10:]}

    def set_risk_factor(self, pct: float, why: str) -> dict:
        """Change the per-trade risk lever. Guard-enforced, not a convention: refuses outside
        [RISK_FACTOR_MIN, MAX_RISK_PER_TRADE] regardless of who asks or why. Persists across windows
        on purpose -- this is meant to move over the account's whole life, not reset every attempt."""
        if len(why.strip()) < 10:
            return {"ok": False, "reason": "a reason is required (at least 10 characters) in --why"}
        if not (R.RISK_FACTOR_MIN - 1e-12 <= pct <= R.MAX_RISK_PER_TRADE + 1e-12):
            return {"ok": False, "reason": f"risk_pct {pct} is outside the guard's allowed range "
                                           f"[{R.RISK_FACTOR_MIN}, {R.MAX_RISK_PER_TRADE}]"}
        old = self.s.get("risk_pct", R.RISK_FACTOR_DEFAULT)
        self.s["risk_pct"] = pct
        entry = {"utc": self.cur_time(), "old": old, "new": pct, "why": why.strip()}
        self.s.setdefault("risk_factor_history", []).append(entry)
        self.log("risk_factor_changed", old_pct=old, new_pct=pct, why=why.strip())
        self.save()
        return {"ok": True, "old": old, "new": pct, "per_trade_cap_usd": round(pct * R.START_BALANCE, 2)}

    def journal(self, text: str) -> dict:
        if len(text.strip()) < 10:
            return {"ok": False, "reason": "a note needs at least 10 characters"}
        self.s["notes_count"] += 1
        days = self.s.setdefault("note_days", [])
        if self.s["day"] not in days:
            days.append(self.s["day"])
        self.log("note", day=self.s["day"], text=text.strip())
        self.save()
        return {"ok": True, "notes_count": self.s["notes_count"]}

    def fitness(self) -> dict:
        closes = []
        if self.ledger_path.exists():
            with open(self.ledger_path) as f:
                closes = [json.loads(l) for l in f if '"trade_closed"' in l]
        current = self.s.get("attempt", 1)
        closes = [c for c in closes if c.get("type") == "trade_closed" and c.get("attempt", 1) == current]
        return fitness_components(closes, self.s.get("start_day"), self.s.get("day"), R.START_BALANCE)

    # ---- replay (driver only) --------------------------------------------
    def reload(self):
        """Re-read the account state from disk -- the agent's own paper.py calls (other processes) wrote
        it while the driver's demands ran. The feeds, timeline and windows never change, so a long-running
        driver keeps them instead of re-reading every symbol's candles from disk (~7 s) at every stop."""
        if self.state_path.exists():
            self.s = json.loads(self.state_path.read_text())
        self._jump_cursor = None
        self._stop_reason = None

    def _bar(self) -> bool:
        """Step ONE bar. False at the end of the replay."""
        if self.s["cursor"] >= len(self.timeline):
            return False
        self._step(self.timeline[self.s["cursor"]])
        if self._jump_cursor is not None:
            self.s["cursor"] = self._jump_cursor
            self._jump_cursor = None
        else:
            self.s["cursor"] += 1
        return True

    def advance(self, n: int) -> dict:
        """Steps forward n bars, saving after EVERY bar (not once at the end), so a dashboard reading
        account.json sees each trade as it closes. Never stops early except at the replay's end; the
        driver uses advance_to_decision() instead. The hourly in-window scan still runs (on the clock,
        see _hourly_scan), so a caller stepping by hand sees the same setup_candidate events."""
        before = self._outbox_len()
        processed = 0
        for _ in range(n):
            if not self._bar():
                break
            processed += 1
            t = self.t_now()
            if t is not None:
                self._clock(t)   # the same clock as advance_to_decision: turns and screens are logged/flagged
            self.save()
        return {"processed": processed, "cursor": self.s["cursor"], "events_added": self._outbox_len() - before}

    def _clock(self, t: int) -> tuple[str, list | None]:
        """One bar of the live desk's clock (tools/live_loop.py tick): ('reassess', movers) on the first bar of a
        new decision slot, ('scan', fresh) when the move screen flags a symbol not flagged in the last
        REFLAG_AFTER_MIN, else ('', None)."""
        close_t = t + 300
        managed = self._manage(t)        # logged to the outbox now; it stops the replay if nothing else does
        flags = expire_flags(self.s.get("scan_flags") or {}, close_t)
        self.s["scan_flags"] = flags
        self._risk_checkpoint(t)                # window end: the risk worker's checkpoint (logged, routed at the next stop)
        if self._rails_blocked_now():
            # 2026-11-01: while a rail refuses every order nobody is woken for an entry (live's screen skips too; on 06-24/25 a
            # day of scouts and deciders ran into a halt). The manager still wakes; the slot is spent so no turn fires on release.
            if train_decision_slot(t) is not None:
                self.s["decided_slot"] = train_decision_slot(t)
            return ("manage", managed) if managed else ("", None)
        key = MC.swing_key(bar_utc(close_t))    # the once-a-day swing turn (live_loop.swing_event, same clock)
        if key and key != self.s.get("swing_day") and self.s.get("breached") is None:
            self.s["swing_day"] = key
            return "swing", self.day_movers(t)
        slot = train_decision_slot(t)
        if slot is not None and slot != self.s.get("decided_slot"):
            self.s["decided_slot"] = slot
            movers = self.scan_candidates(threshold_pct=0.0)[:DECISION_MOVERS]
            for c in movers:            # the decider has seen them: restart their hour (live _flag restart=True)
                if abs(c["pct_move"]) >= SCAN_THRESHOLD_PCT:
                    flags[c["symbol"]] = close_t
            return "reassess", movers
        if in_train_window(t):
            fresh = self._maybe_flag_setup_candidates(close_t)
            if fresh:
                return "scan", fresh
        if managed:
            return "manage", managed
        return "", None

    def _rails_blocked_now(self) -> bool:
        """A day-loss or giveback halt (not the position cap: positions close and the desk has decisions to make about them)."""
        b = self._own_rails_block() or ""
        return b.startswith(("day-loss", "peak-giveback"))

    def _at_extreme(self, c: dict) -> bool:
        """The move sits in the top 20% of the last 6 hours' range in its own direction. Mid-range movers are what scouts mostly
        dismiss, and they ran on no more often (2026-11-01, 919 graded movers: 26% vs 31% ran 3+ ATR). Training's cost filter,
        not an edge: the decision turn still sees every mover. Live's screen is unchanged."""
        rows = self.recent_bars(c["symbol"], 72, "M5")
        if len(rows) < 12:
            return True
        hi, lo = max(r["h"] for r in rows), min(r["l"] for r in rows)
        if hi <= lo:
            return True
        pos = (rows[-1]["c"] - lo) / (hi - lo)
        return (pos if c["pct_move"] > 0 else 1 - pos) >= 0.8

    def _risk_checkpoint(self, t: int):
        """The first bar at or after the window's end on a weekday: risk_review for the risk kind if trades closed that
        day, and every Friday (live_loop's live_risk_review rule, same terms)."""
        loc = bar_close_local(t)
        if loc.weekday() >= 5 or loc.hour * 60 + loc.minute < TRAIN_WINDOW_END_MIN:
            return
        day = loc.date().isoformat()
        if self.s.get("risk_checked_day") == day:
            return
        self.s["risk_checked_day"] = day
        rows = [r for r in self._ledger_tail(4000) if r.get("type") == "trade_closed"]
        today = [r for r in rows if str(r.get("closed_utc") or "")[:10] == bar_utc(t).date().isoformat()]
        if not today and loc.weekday() != 4:
            return
        stat = lambda xs: {"n": len(xs), "wins": sum(1 for r in xs if (r.get("net_usd") or 0) > 0),
                           "mean_r": round(sum(r.get("r_net") or 0 for r in xs) / len(xs), 3) if xs else None,
                           "net_usd": round(sum(r.get("net_usd") or 0 for r in xs), 2)}
        self.log("risk_review", day=day, weekly=loc.weekday() == 4, closed_today=len(today), today=stat(today),
                 last_20=stat(rows[-20:]), all_time=stat(rows), lever=self.risk_factor())

    def day_movers(self, t: int) -> list[dict]:
        """Each symbol's move since the Prague day began, as live_loop.swing_event measures it (manage_core.day_movers)."""
        close = bar_utc(t + 300)
        start = close.astimezone(ZoneInfo(R.DAY_TZ)).replace(hour=0, minute=0, second=0, microsecond=0)
        rows = {sym: self.recent_bars(sym, 288, "M5") for sym in self.feeds if self.s["last_close"].get(sym) is not None}
        return MC.day_movers(rows, start.astimezone(timezone.utc))

    def _manage(self, t: int) -> list[dict]:
        """THE MANAGER'S WAKE on the replay (2026-10-19): every open position, every bar, through the live loop's
        own rules (tools/manage_core.py) -- the same triggers from the same kind of bars. A trigger logs
        manage_wake (routed to the manage kind) and stops the replay there."""
        if not self.s["positions"]:
            return []
        now = bar_utc(t + 300)
        inw = in_train_window(t)
        roll = MC.rollover_utc(now)
        fri = MC.friday_flat_utc(now, MC.FRI_FLAT_HOUR_UTC)
        st_all = self.s.setdefault("manage", {})
        out = []
        for p in list(self.s["positions"]):
            try:
                opened = datetime.strptime(p["opened_utc"][:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            n5 = min(300, max(3, int((now - opened).total_seconds() // 300) + 3))
            m5 = self.recent_bars(p["symbol"], n5, "M5")
            m15 = self.recent_bars(p["symbol"], MC.BOS_LOOKBACK + n5 // 3 + 3, "M15")
            hist = None if inw else self.recent_bars(p["symbol"], MC.HISTORY_M5, "M5")
            pos = {k: p.get(k) for k in ("id", "symbol", "side", "lots", "entry", "sl", "tp", "opened_utc", "why")}
            pos["sl0"] = p.get("sl0") or p["sl"]
            trig, st, info = MC.evaluate(pos, m5, m15, now, in_window=inw, rollover_utc=roll, friday_flat_utc=fri,
                                         st=st_all.get(str(p["id"])), m5_hist=hist, day=self.s.get("day"))
            st_all[str(p["id"])] = st
            if trig:
                out.append(self.log("manage_wake", at_utc=now.strftime("%Y-%m-%d %H:%M"), id=p["id"], symbol=p["symbol"],
                                    side=p["side"], lots=p["lots"],
                                    entry=p["entry"], sl=p["sl"], sl0=pos["sl0"], tp=p.get("tp"),
                                    opened_utc=p["opened_utc"], why=p.get("why"), triggers=trig, info=info))
        return out

    def advance_to_decision(self, max_bars: int = 288) -> dict:
        """Steps forward until a decision belongs here (THE DECISION CLOCK at the top: the live desk's clock),
        at most max_bars. stop is one of: reassess (a decision turn; movers = the last hour's top movers),
        scan (the move screen flagged a mover -> setup_candidate in the outbox), alert (a price alert's
        condition met -> level_alert in the outbox), risk_warning / rule_breach, cap (max_bars with no
        decision, e.g. a holiday), end (the replay ran out)."""
        before = self._outbox_len()
        processed, stop, movers = 0, "cap", None
        self._stop_reason = None
        while processed < max_bars:
            if not self._bar():
                stop = "end"
                break
            processed += 1
            t = self.t_now()
            if t is not None:
                what, got = self._clock(t)
                if what == "reassess":
                    stop, movers = "reassess", got
                elif what == "scan":
                    stop = "scan"
                elif what == "manage":
                    stop = "manage"
                elif what == "swing":
                    stop, movers = "swing", got
            if stop == "cap" and self._stop_reason:
                stop = self._stop_reason
            self.save()
            if stop != "cap":
                break
        out = {"processed": processed, "cursor": self.s["cursor"], "events_added": self._outbox_len() - before,
               "stop": stop, "at": self.cur_time()}
        if movers is not None:
            out["movers"] = movers
            out["next_decision_utc"] = train_next_decision_utc(self.t_now()) if self.t_now() else None
        return out

    def _symbol_ts(self, sym: str) -> list[int]:
        """Each feed's timestamps, built once: rebuilding a list of up to ~2M ints per symbol on
        every hourly scan was where most of the replay's CPU went."""
        cache = self.__dict__.setdefault("_ts_cache", {})
        if sym not in cache:
            cache[sym] = [b["t"] for b in self.feeds[sym]]
        return cache[sym]

    def scan_candidates(self, lookback_bars: int = SCAN_EVERY_BARS, threshold_pct: float = SCAN_THRESHOLD_PCT
                        ) -> list[dict]:
        """Cheap, pure-code screen across every replayable symbol: who moved more than threshold_pct%
        over the last lookback_bars M5 bars. No Claude call, no board post, no resample of the whole
        history -- one bisect + a slice per symbol, so calling this often is genuinely free. This is
        what lets an hourly tick decide whether anything is even worth a scout look, instead of paying
        for a model call every single hour regardless of whether anything happened. A symbol whose
        last bar is over 20 minutes old (its market is shut) is skipped, as live_loop does."""
        t = self.t_now()
        if t is None:
            return []
        out = []
        for sym, bars in self.feeds.items():
            i = bisect.bisect_right(self._symbol_ts(sym), t)
            if i <= lookback_bars:
                continue
            window = bars[i - lookback_bars - 1:i]
            if t - window[-1]["t"] > 20 * 60:
                continue
            start_c, end_c = window[0]["c"], window[-1]["c"]
            if start_c <= 0:
                continue
            pct = (end_c - start_c) / start_c * 100
            if abs(pct) >= threshold_pct:
                out.append({"symbol": sym, "pct_move": round(pct, 3), "bars": lookback_bars})
        out.sort(key=lambda x: -abs(x["pct_move"]))
        return out

    def _maybe_flag_setup_candidates(self, close_t: int | None = None) -> list[dict]:
        """The move screen (live_loop tick's screen branch): logs -- and thereby routes to a scout -- the symbols
        past the threshold that are not flagged right now, and flags them for REFLAG_AFTER_MIN. Returns what it
        flagged ([] = nothing to wake anyone for)."""
        t = self.t_now()
        close_t = close_t if close_t is not None else (t + 300 if t is not None else 0)
        flags = self.s.setdefault("scan_flags", {})
        fresh = [c for c in self.scan_candidates() if c["symbol"] not in flags and self._at_extreme(c)][:SCAN_MAX]
        if not fresh:
            return []
        for c in fresh:
            flags[c["symbol"]] = close_t
        import desk_shared as DS
        fresh = DS.collapse_twins(fresh)   # 2026-11-01: one correlated move wakes one scout (live_loop: the same)
        self.log("setup_candidate", candidates=fresh)
        return fresh

    # ---- price alerts: "wake me when price gets there" at zero cost until it does ----------------
    def _atr_m15(self, symbol: str) -> float:
        return AC.atr(self.recent_bars(symbol, 15, "M15"))

    def set_alert(self, symbol: str, above, below, why: str, hours: float = 8.0, worker: str = "trade",
                  when: str = "touch", tf: str = "M15", nbars: int = 2, sl=None, tp=None,
                  require_plan: bool = False) -> dict:
        """A level the agent wants to act at LATER (a retest zone, a break level). Pure code watches it on
        every M5 bar; inside the trading window, the first bar that meets its condition stops the replay
        and wakes that symbol's trader (or the generalist) with this plan -- instead of a chase now, or a
        model call every hour to check. The rules are tools/alerts_core.py, shared with the live desk."""
        t = self.t_now()
        if t is None:
            return {"ok": False, "reason": "no bars yet; the replay has not started"}
        if symbol not in self.feeds:
            return {"ok": False, "reason": f"{symbol} is not replayable. Replayable: {sorted(self.feeds)}"}
        active = self.s.setdefault("alerts", [])
        if when != "touch" and tf == "M5":
            tf = "M15"   # 2026-11-01: alert-born trades on an M5 trigger n=6, -0.82R; on M15 n=7, +0.19R. A 5-minute candle is not a level holding
        err, spec = AC.check_new(symbol, above, below, why, self.s["last_close"].get(symbol), self._atr_m15(symbol),
                                 when, tf, nbars, hours, active, sl, tp, require_plan)
        if err:
            return {"ok": False, "reason": err}
        aid = self.s.get("next_alert_id", 1)
        self.s["next_alert_id"] = aid + 1
        expires_t = int(t + spec.pop("hours") * 3600)
        rec = {"id": aid, **spec, "by": worker, "set_utc": self.cur_time(), "set_t": t, "expires_t": expires_t,
               "expires_utc": bar_utc(expires_t).strftime("%Y-%m-%d %H:%M UTC")}
        active.append(rec)
        self.log("alert_set", **{k: rec[k] for k in ("id", "symbol", "dir", "level", "why", "by", "when", "tf", "nbars",
                                                     "ref", "expires_utc", "plan_side", "plan_sl", "plan_tp")})
        self.save()
        return {"ok": True, "id": aid, "fires": f"{self._alert_text(rec)} inside the window, before {rec['expires_utc']}",
                "wakes": "that symbol's trader if it has one, else the generalist trade turn, with your --why as the plan"}

    def cancel_alert(self, aid: int, why: str) -> dict:
        active = self.s.setdefault("alerts", [])
        a = next((x for x in active if x["id"] == aid), None)
        if a is None:
            return {"ok": False, "reason": f"no waiting alert {aid}; waiting: {[x['id'] for x in active]}"}
        active.remove(a)
        self.log("alert_cancelled", id=aid, symbol=a["symbol"], why=why.strip())
        self.save()
        return {"ok": True, "cancelled": aid}

    @staticmethod
    def _alert_text(a: dict) -> str:
        return AC.text(a)

    def _tf_bar(self, sym: str, t: int, tf: str) -> dict | None:
        """The tf bar that the M5 bar opening at t completes, or None if t does not close a tf bucket."""
        sec = TF_SECONDS[tf]
        if (t + 300) % sec:
            return None
        ts = self._symbol_ts(sym)
        grp = self.feeds[sym][bisect.bisect_left(ts, t - sec + 300):bisect.bisect_right(ts, t)]
        if not grp:
            return None
        return {"o": grp[0]["o"], "h": max(x["h"] for x in grp), "l": min(x["l"] for x in grp), "c": grp[-1]["c"]}

    def _check_alerts(self, t: int, present: dict):
        active = self.s.get("alerts") or []
        if not active:
            return
        for a in list(active):
            if t > a["expires_t"]:
                active.remove(a)
                self.log("alert_expired", id=a["id"], symbol=a["symbol"], level=a["level"])
                continue
            b = present.get(a["symbol"])
            if b is None:
                continue
            bar = b if a.get("when", "touch") == "touch" else self._tf_bar(a["symbol"], t, a.get("tf", "M15"))
            if bar is None:
                continue
            hit = AC.step(a, bar)
            if hit and (not in_train_window(t) or self._rails_blocked_now()):
                AC.outside_window(a)
                continue
            if hit:
                active.remove(a)
                self.log("level_alert", **{k: a.get(k) for k in AC.HIT_KEYS},
                         condition=self._alert_text(a), bar={"o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"]},
                         touched_utc=bar_utc(t).strftime("%Y-%m-%d %H:%M UTC"))
                self._stop_reason = self._stop_reason or "alert"

    def _outbox_len(self) -> int:
        if not self.outbox_path.exists():
            return 0
        with open(self.outbox_path) as f:
            return sum(1 for _ in f)

    def _step(self, t: int):
        dk = day_key(t)
        present = {sym: feed[t] for sym, feed in self.by_t.items() if t in feed}
        if self.s["day"] is None:
            self.s["day"] = dk
            self.s["start_day"] = dk
            self.s["day_start_equity"] = self.equity_now()
        elif dk != self.s["day"]:
            self._end_day()
            self.s["day"] = dk
            self.s["day_start_equity"] = self.equity_now()
            self.s["warned"] = []
            self.s["scan_flags"] = {}
            # 2026-11-01: the giveback halt is per trading day. With no deadline an attempt ends only by pass or breach, so a
            # halt from the ATTEMPT's peak blocked every order from then on (06-24: 5 refused orders, a day of calls for nothing)
            self.s["peak_equity"] = self.equity_now()

        for o in sorted(list(self.s["pending"]), key=lambda x: x["id"]):
            if o["symbol"] not in present:
                continue
            b = present[o["symbol"]]
            if o["kind"] == "close":
                p = next((x for x in self.s["positions"] if x["id"] == o["id"]), None)
                if p is not None and o.get("volume") and o["volume"] < p["lots"] - 1e-9:
                    self._partial(p, self.exit_mark(p, b["o"]), o["volume"], o["why"])
                elif p is not None:
                    self._close(p["id"], self.exit_mark(p, b["o"]), "manual", o["why"])
                self.s["pending"] = [x for x in self.s["pending"] if x["id"] != o["id"]]
            else:
                self._fill_open(o, b, t)

        for p in list(self.s["positions"]):
            if p["symbol"] in present:
                self._check_levels(p, present[p["symbol"]])

        for sym, b in present.items():
            self.s["last_close"][sym] = b["c"]
        self._check_alerts(t, present)
        self.s["equity"] = self.equity_now()
        self.s["peak_equity"] = max(float(self.s.get("peak_equity") or R.START_BALANCE), self.s["equity"])
        if self.s["breached"] is None:
            self._rule_check()
        if self.s["breached"] is not None:
            if self.windows:  # a real replay always has windows; a tiny test fixture might not
                self._end_window(f"breached:{self.s['breached']}")
            return
        if self._next_bar_starts_a_weekend(t):
            self._flatten("weekend_flatten")
        target_met = (self.s["equity"] >= R.START_BALANCE * (1 + R.CHALLENGE["profit_target"])
                      and len(self.s["traded_days"]) >= R.CHALLENGE["min_trading_days"])
        if self.deadline:                               # the old demo window (kept switchable, off since 2026-10-18)
            if self.windows and self._is_last_bar_of_window(t):
                self._end_window("passed" if target_met else "window_end")
        elif target_met:                                # the live terms: passed the moment the target is met
            self._end_window("passed")
        elif self.windows and self.s["cursor"] + 1 >= len(self.timeline):
            self._end_window("data_end")                # the history ran out mid-attempt: the epoch wraps

    def _next_bar_starts_a_weekend(self, t: int) -> bool:
        idx = self.s["cursor"]
        if idx + 1 >= len(self.timeline):
            return False
        return date.fromisoformat(day_key(t)).weekday() != 0 \
            and date.fromisoformat(day_key(self.timeline[idx + 1])).weekday() == 0

    def _is_last_bar_of_window(self, t: int) -> bool:
        _, end_t = self.windows[self.s["window_index"] % len(self.windows)]
        idx = self.s["cursor"]
        return idx + 1 >= len(self.timeline) or self.timeline[idx + 1] >= end_t

    def _flatten(self, reason: str):
        for p in list(self.s["positions"]):
            self._close(p["id"], self.exit_mark(p, self.mark(p)), reason, f"{reason}: no holding over the weekend")
        self.s["pending"] = []

    def _fill_open(self, o: dict, b: dict, t: int):
        self.s["pending"] = [x for x in self.s["pending"] if x["id"] != o["id"]]
        fill = b["o"] + self.half_spread(o["symbol"]) if o["side"] == "buy" else b["o"] - self.half_spread(o["symbol"])
        state = self._goal_state() | {"equity": self.s["equity"], "day_start_equity": self.s["day_start_equity"],
                                      "breached": None}
        ok, reason = R.order_check(state, {"symbol": o["symbol"], "side": o["side"], "lots": o["lots"],
                                           "sl": o["sl"], "tp": o["tp"]}, fill, self.open_risk(), 0.0)
        if not ok:
            self.log("order_rejected", id=o["id"], symbol=o["symbol"], side=o["side"], why=o["why"], reason=reason)
            return
        risk = R.risk_usd(o["symbol"], o["lots"], fill, o["sl"])
        pos = {"id": o["id"], "symbol": o["symbol"], "side": o["side"], "lots": o["lots"], "entry": round(fill, 6),
               "sl": o["sl"], "tp": o["tp"], "why": o["why"], "risk_usd": round(risk, 2),
               "sl0": o["sl"], "risk_usd0": round(risk, 2), "lots0": o["lots"], "realized_usd": 0.0,
               "opened_utc": bar_utc(t).strftime("%Y-%m-%d %H:%M"), "opened_t": t, "day": self.s["day"],
               "consulted_analysis": o.get("consulted_analysis", False)}
        self.s["positions"].append(pos)
        if self.s["day"] not in self.s["traded_days"]:
            self.s["traded_days"].append(self.s["day"])
        self.log("trade_opened", **{k: pos[k] for k in ("id", "symbol", "side", "lots", "entry", "sl", "tp", "why",
                                                         "risk_usd", "opened_utc", "consulted_analysis")})

    def _check_levels(self, p: dict, b: dict):
        buy = p["side"] == "buy"
        sl_hit = (b["l"] <= p["sl"]) if buy else (b["h"] >= p["sl"])
        tp_hit = p["tp"] is not None and ((b["h"] >= p["tp"]) if buy else (b["l"] <= p["tp"]))
        if sl_hit:
            level = p["sl"] if (b["o"] >= p["sl"] if buy else b["o"] <= p["sl"]) else b["o"]
            self._close(p["id"], self.exit_mark(p, level), "sl", "stop hit")
        elif tp_hit:
            level = p["tp"] if (b["o"] <= p["tp"] if buy else b["o"] >= p["tp"]) else b["o"]
            self._close(p["id"], self.exit_mark(p, level), "tp", "target hit")

    def _partial(self, p: dict, price: float, volume: float, why: str):
        """Bank `volume` lots of an open position at `price` (the live desk's close --volume): the rest stays open
        with its stop and target; the trade's final trade_closed carries the whole result."""
        gross = self.pnl_usd({**p, "lots": volume}, price)
        net = gross - R.spec(p["symbol"])["commission_rt_per_lot"] * volume
        self.s["balance"] += net
        before = p["lots"]
        p["lots"] = round(before - volume, 8)
        p["risk_usd"] = round(p["risk_usd"] * p["lots"] / before, 2) if before else p["risk_usd"]
        p["realized_usd"] = round(float(p.get("realized_usd") or 0.0) + net, 2)
        p.setdefault("partials", []).append({"utc": self.cur_time(), "lots": volume, "price": round(price, 6),
                                             "net_usd": round(net, 2)})
        self.log("partial_closed", id=p["id"], symbol=p["symbol"], lots=volume, remaining=p["lots"],
                 price=round(price, 6), net_usd=round(net, 2), why=why)

    def _close(self, pid: int, price: float, reason: str, why: str):
        p = next(x for x in self.s["positions"] if x["id"] == pid)
        self.s["positions"].remove(p)
        gross = self.pnl_usd(p, price)
        comm = R.spec(p["symbol"])["commission_rt_per_lot"] * p["lots"]
        self.s["balance"] += gross - comm
        self.s.get("manage", {}).pop(str(pid), None)
        net = gross - comm + float(p.get("realized_usd") or 0.0)   # a partial close already banked its part
        result = "win" if net > 0 else ("loss" if net < 0 else "flat")
        if result == "loss":
            self.s["consec_losses"] += 1
        elif result == "win":
            self.s["consec_losses"] = 0
        side_word = "long" if p["side"] == "buy" else "short"
        r_price = R.chart_r_multiple(side_word, p["entry"], round(price, 6), p.get("sl0") or p["sl"])
        risk0 = p.get("risk_usd0") or p["risk_usd"]
        r_net = round(net / risk0, 3) if risk0 else None
        self.s["pending"] = [o for o in self.s["pending"] if not (o["kind"] == "close" and o["id"] == pid)]
        self.log("trade_closed", attempt=self.s.get("attempt", 1), id=pid, symbol=p["symbol"], side=p["side"],
                 lots=p.get("lots0") or p["lots"], entry=p["entry"], exit=round(price, 6), sl=p["sl"], tp=p["tp"],
                 initial_sl=p.get("sl0") or p["sl"], reason=reason,
                 net_usd=round(net, 2), r_multiple=r_price, r_price=r_price, r_net=r_net, result=result,
                 risk_usd=risk0, why_open=p["why"], close_note=why, opened_utc=p["opened_utc"],
                 partial_closes=p.get("partials") or [],
                 closed_utc=self.cur_time(), consecutive_losses=self.s["consec_losses"],
                 balance=round(self.s["balance"], 2), consulted_analysis=p.get("consulted_analysis", False))

    def _rule_check(self):
        eq = self.s["equity"]
        day_used = self.s["day_start_equity"] - eq
        total_used = R.START_BALANCE - eq
        if day_used >= R.daily_loss_limit_usd():
            self._breach("daily_loss")
            return
        if total_used >= R.total_loss_limit_usd():
            self._breach("total_loss")
            return
        for kind, used, limit in (("daily", day_used, R.daily_loss_limit_usd()),
                                  ("total", total_used, R.total_loss_limit_usd())):
            key = f"{self.s['day']}:{kind}"
            if used >= R.WARN_FRACTION * limit and key not in self.s["warned"]:
                self.s["warned"].append(key)
                self.s["risk_warnings_this_attempt"] = self.s.get("risk_warnings_this_attempt", 0) + 1
                self.log("risk_warning", kind=kind, used_usd=round(used, 2), limit_usd=round(limit, 2),
                         used_pct_of_limit=round(100 * used / limit, 1), equity=round(eq, 2))

    def _breach(self, kind: str):
        for p in list(self.s["positions"]):
            self._close(p["id"], self.exit_mark(p, self.mark(p)), "breach", f"closed by {kind} breach")
        self.s["pending"] = []
        self.s["equity"] = self.equity_now()
        self.s["breached"] = kind
        self.log("rule_breach", kind=kind, equity=round(self.s["equity"], 2), day=self.s["day"])

    def _end_day(self):
        eq = self.s["equity"]
        traded = self.s["day"] in self.s["traded_days"]
        notes_today = sum(1 for _ in self._notes_for(self.s["day"]))
        self.log("day_closed", day=self.s["day"], day_pnl=round(eq - self.s["day_start_equity"], 2),
                 day_pnl_pct=round(100 * (eq - self.s["day_start_equity"]) / R.START_BALANCE, 3),
                 traded=traded, equity_end=round(eq, 2), journal_notes_today=notes_today,
                 traded_days_total=len(self.s["traded_days"]), consecutive_losses=self.s["consec_losses"])
        for g in self.goals():
            if g["passed"] and g["goal"] not in self.s["goals_passed"]:
                self.s["goals_passed"].append(g["goal"])
                self.log("goal_passed", goal=g["goal"], evidence=g["evidence"])

    def _end_window(self, outcome: str):
        """A window is over: passed, breached:<kind>, or window_end. Logs it, updates the running pass rate,
        fires the self-trigger events, advances attempt/window/epoch, and resets the account for the next one.

        The force-close below is DEMO ONLY (guard/rules.py DEMO_AUTO_CLOSE_AT_WINDOW_END) and mechanical:
        the demo account is resetting, there is nothing to hold into the next one, and no kind decided
        this -- the program did, the instant the window ended, before anything else in this function
        runs. routing.py tells the trade_closed review for reason="attempt_end" exactly that, so it is
        never mistaken for a trading decision. Flip the flag for a live deployment, where there is no
        window to reset and a position may need to be managed past this boundary instead."""
        if R.DEMO_AUTO_CLOSE_AT_WINDOW_END:
            for p in list(self.s["positions"]):
                self._close(p["id"], self.exit_mark(p, self.mark(p)), "attempt_end", f"attempt ended: {outcome}")
        self.s["pending"] = []
        eq = self.equity_now()
        passed = outcome == "passed"
        breached = outcome.startswith("breached")
        # "came close" even without breaching: any risk_warning fired this attempt, or ended having
        # given back most of the allowed total loss. Either earns the SAME deep look a breach gets --
        # the operator's point: a near-miss and a pass both teach something, not just an outright fail.
        total_used_frac = max(0.0, (R.START_BALANCE - eq) / (R.START_BALANCE * R.CHALLENGE["max_total_loss"]))
        near_miss = (not passed and not breached
                     and (self.s.get("risk_warnings_this_attempt", 0) > 0 or total_used_frac >= 0.7))
        # .setdefault, not a bare literal: an account that started before these two fields existed
        # (any already-running live account) has neither key in its loaded state at all, and the
        # "else" branch below never sets best_pass_streak -- a plain self.s["best_pass_streak"]
        # read after it would KeyError on exactly that account. Confirmed live, 2026-10-09.
        self.s.setdefault("consecutive_passes", 0)
        self.s.setdefault("best_pass_streak", 0)
        if passed:
            self.s["consecutive_passes"] += 1
            self.s["best_pass_streak"] = max(self.s["best_pass_streak"], self.s["consecutive_passes"])
        else:
            self.s["consecutive_passes"] = 0
        self.log("attempt_ended", attempt=self.s["attempt"], epoch=self.s["epoch"], window_index=self.s["window_index"],
                 outcome=outcome, final_equity=round(eq, 2), traded_days=len(self.s["traded_days"]),
                 start_day=self.s.get("start_day"), end_day=self.s["day"], near_miss=near_miss,
                 consecutive_passes=self.s["consecutive_passes"], best_pass_streak=self.s["best_pass_streak"],
                 risk_warnings_this_attempt=self.s.get("risk_warnings_this_attempt", 0))
        self.s["attempts_total"] += 1
        if passed:
            self.s["attempts_passed"] += 1
            self.log("attempt_passed_review", attempt=self.s["attempt"], final_equity=round(eq, 2),
                     traded_days=len(self.s["traded_days"]), consecutive_passes=self.s["consecutive_passes"],
                     best_pass_streak=self.s["best_pass_streak"])
        elif breached or near_miss:
            self.log("attempt_breach_review", attempt=self.s["attempt"], outcome=outcome,
                     final_equity=round(eq, 2), traded_days=len(self.s["traded_days"]), near_miss=near_miss,
                     risk_warnings_this_attempt=self.s.get("risk_warnings_this_attempt", 0),
                     start_day=self.s.get("start_day"), end_day=self.s["day"])
        rate = self.s["attempts_passed"] / self.s["attempts_total"]
        for m in R.PASS_RATE_MILESTONES:
            if rate >= m and m not in self.s["milestones_fired"]:
                self.s["milestones_fired"].append(m)
                self.log("pass_rate_milestone", milestone=m, pass_rate=round(rate, 4),
                         attempts_total=self.s["attempts_total"], attempts_passed=self.s["attempts_passed"])
        if self.deadline:
            self.s["window_index"] += 1
            wrap = self.s["window_index"] >= len(self.windows)
        else:   # no deadline: the next attempt starts on the next bar, unless the history ran out
            wrap = self.s["cursor"] + 1 >= len(self.timeline)
        if wrap:
            self.log("epoch_review", epoch=self.s["epoch"], attempts_total=self.s["attempts_total"],
                     attempts_passed=self.s["attempts_passed"], pass_rate=round(rate, 4))
            self.s["window_index"] = 0
            self.s["epoch"] += 1
        self.s["attempt"] += 1
        if self.deadline or wrap:
            self._jump_cursor = self._index_of(self.windows[self.s["window_index"]][0])
        self.s.update({"balance": R.START_BALANCE, "equity": R.START_BALANCE, "traded_days": [], "note_days": [],
                       "goals_passed": [], "warned": [], "consec_losses": 0, "breached": None, "day": None,
                       "start_day": None, "risk_warnings_this_attempt": 0, "alerts": [],
                       "peak_equity": R.START_BALANCE})
        # consecutive_passes/best_pass_streak are NOT reset here, same as risk_pct: continuity across
        # windows is the entire point of tracking them.

    def visible_ledger(self) -> list[dict]:
        """Every ledger row whose own time is not after NOW: the most recently written row's time.
        After an epoch wraps, earlier rows from a LATER window of an EARLIER epoch are in the file
        but describe the future relative to where the replay currently sits -- never show those."""
        if not self.ledger_path.exists():
            return []
        with open(self.ledger_path) as f:
            rows = [json.loads(l) for l in f if l.strip()]
        if not rows:
            return rows
        now = rows[-1]["utc"]  # the last row written IS the current simulated time, by construction
        return [r for r in rows if r.get("utc", now) <= now]

    def _notes_for(self, day: str):
        if not self.ledger_path.exists():
            return
        with open(self.ledger_path) as f:
            for line in f:
                r = json.loads(line)
                if r["type"] == "note" and r.get("day") == day:
                    yield r


# 2026-10-18: reviews read `ledger -n 60` every time (18-25k characters); 43% of it was alert rows carrying the
# same plan text twice and 10% scan rows. The default view keeps the decisions; --all keeps everything.
LEDGER_NOISE = {"setup_candidate", "alert_expired", "field_read"}
_BRIEF_DROP = {"bar"}
_ALERT_HIT_DROP = {"why", "ref", "expires_utc", "set_utc", "by", "when", "tf", "nbars", "dir", "level"}


def ledger_brief(r: dict) -> dict:
    drop = _BRIEF_DROP | (_ALERT_HIT_DROP if r.get("type") == "level_alert" else set()) \
        | ({"why"} if r.get("type") == "trade_opened" else set())  # same text as its order_placed row
    return {k: v for k, v in r.items() if k not in drop}


def _out(obj) -> None:
    """Every reply as ONE line of compact JSON (2026-10-18): the indent=1 form spent a newline and a
    space per key in every tool result, and tool results are most of what each call pays for. Same
    fields, same numbers."""
    print(json.dumps(obj, separators=(",", ":"), default=str))


def bars_text(symbol: str, tf: str, rows: list[dict]) -> str:
    """Bars as a table, one line per bar (2026-10-18): the per-bar JSON objects repeated six key names
    and a tf/day field 30 times over, ~4k characters for 30 bars; this is the same data at about a
    third of that. The Prague trading day is printed once where it changes, not on every bar."""
    if not rows:
        return f"{symbol} {tf}: no closed bars yet"
    if "error" in rows[0]:
        return json.dumps(rows[0])
    lines = [f"{symbol} {tf}, last {len(rows)} closed bars. columns: utc_open session open high low close"]
    day = None
    for b in rows:
        if b["day"] != day:
            day = b["day"]
            lines.append(f"# trading day {day} (Prague)")
        lines.append(f"{b['utc']} {b['session'] or '-'} {b['o']} {b['h']} {b['l']} {b['c']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="paper.py", description="paper broker for the FTMO field; paper only")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="account, limits, positions, goals, epoch/window/attempt, pass rate")
    sub.add_parser("field", help="read the field index and mark it read")
    d = sub.add_parser("docs", help="print a manual or field note by name")
    d.add_argument("name")
    sub.add_parser("symbols", help="replayable symbols with specs and last close")
    b = sub.add_parser("bars", help="last N bars of one symbol, any timeframe")
    b.add_argument("symbol")
    b.add_argument("-n", type=int, default=24)
    b.add_argument("--tf", choices=["M5", "M15", "H1", "H4", "D1"], default="M5")
    b.add_argument("--session", choices=["day", "night"], default=None)
    o = sub.add_parser("order", help="place a market order; fills at that symbol's next bar open")
    o.add_argument("--symbol", required=True)
    o.add_argument("--side", choices=["buy", "sell"], required=True)
    o.add_argument("--lots", type=float, required=True)
    o.add_argument("--sl", type=float, required=True)
    o.add_argument("--tp", type=float)
    o.add_argument("--why", required=True)
    c = sub.add_parser("close", help="close an open position (or --volume V lots of it); fills at the next bar open")
    c.add_argument("id", type=int)
    c.add_argument("--symbol", required=True, help="must match that position's own symbol (checked)")
    c.add_argument("--volume", type=float, help="close only this many lots (a partial close)")
    c.add_argument("--why", required=True)
    m = sub.add_parser("modify", help="move a stop toward profit (never wider) and/or the target")
    m.add_argument("id", type=int)
    m.add_argument("--symbol", required=True, help="must match that position's own symbol (checked)")
    m.add_argument("--sl", type=float)
    m.add_argument("--tp", type=float)
    m.add_argument("--why", required=True)
    wt = sub.add_parser("watch", help="the manager's standing watch: re-wake at a level/time, or --quiet (let it rip)")
    wt.add_argument("id", type=int)
    wt.add_argument("--symbol", required=True)
    wt.add_argument("--above", type=float)
    wt.add_argument("--below", type=float)
    wt.add_argument("--in", dest="in_min", type=int, help="re-wake in this many minutes")
    wt.add_argument("--quiet", action="store_true", help="no automatic wake for this position (let it rip)")
    wt.add_argument("--for", dest="quiet_min", type=int, help="quiet for this many minutes (default: until it closes)")
    wt.add_argument("--clear", action="store_true")
    wt.add_argument("--why", default="")
    w = sub.add_parser("widen", help="a WIDER stop: the risk kind only, once per trade")
    w.add_argument("id", type=int)
    w.add_argument("--symbol", required=True)
    w.add_argument("--sl", type=float, required=True)
    w.add_argument("--why", required=True)
    j = sub.add_parser("journal", help="write a journal note for today")
    j.add_argument("--text", required=True)
    rf = sub.add_parser("risk-factor", help="read, or change, the per-trade risk lever (bounded by the guard)")
    rf.add_argument("--set", dest="set_pct", type=float, help="new risk fraction, e.g. 0.005 for 0.5%%")
    rf.add_argument("--why", help="required with --set (>=10 chars)")
    rf.add_argument("--day-halt", dest="day_halt", type=float, help="own day-loss halt, fraction (0.015 = 1.5%%)")
    rf.add_argument("--giveback", type=float, help="own giveback-from-peak halt, fraction")
    rf.add_argument("--max-positions", dest="max_positions", type=int, help="own concurrent-position cap")
    sub.add_parser("goals", help="ladder and process goals, computed by code")
    sub.add_parser("fitness", help="quality, consistency, stalls, drawdown measured from this attempt's ledger")
    t = sub.add_parser("ledger", help="last N ledger events (decisions by default; --all for every row)")
    t.add_argument("-n", type=int, default=20)
    t.add_argument("--all", action="store_true", help="include scan, alert-expiry and bookkeeping rows")
    t.add_argument("--types", help="only these types, comma separated (e.g. trade_closed,note)")
    at = sub.add_parser("attempts", help="how the last N attempts (2-week runs) actually ended")
    at.add_argument("-n", type=int, default=8)
    sub.add_parser("real-eval", help="chances earned at the real two-step FTMO evaluation this demo is practice for")
    al = sub.add_parser("alert", help="wake a trader when price reaches a level (inside the trading window)")
    al.add_argument("--symbol")
    al.add_argument("--above", type=float)
    al.add_argument("--below", type=float)
    al.add_argument("--hours", type=float, default=8.0, help="expires after this many sim hours (0.5-24, default 8)")
    al.add_argument("--when", choices=["touch", "close", "hold", "reject"], default="touch",
                    help="touch = any trade at the level; close/hold/reject = judged on closed --tf bars")
    al.add_argument("--tf", choices=["M5", "M15", "H1"], default="M15")
    al.add_argument("--bars", type=int, default=2, help="hold: consecutive closes needed (1-6)")
    al.add_argument("--from", dest="worker", default="trade")
    al.add_argument("--cancel", type=int, help="cancel the waiting alert with this id")
    al.add_argument("--why", default="")
    al.add_argument("--sl", type=float, help="the plan's mock stop (required for a new alert)")
    al.add_argument("--tp", type=float, help="the plan's mock target (required for a new alert)")
    sub.add_parser("alerts", help="the price alerts waiting now")
    a = p.parse_args(argv)

    paper = Paper()
    if a.cmd == "status":
        _out(paper.snapshot())
    elif a.cmd == "field":
        paper.mark_field_read()
        idx = HOME / "field" / "INDEX.md"
        print(idx.read_text() if idx.exists() else "field not built yet")
    elif a.cmd == "docs":
        name = a.name.replace("..", "")
        for base in (HOME / "manual", HOME / "field", HOME / "knowledge"):
            path = base / (name if name.endswith((".md", ".json")) else name + ".md")
            if path.exists():
                print(path.read_text())
                break
        else:
            print(f"no doc named {a.name}")
    elif a.cmd == "symbols":
        _out(paper.symbols())
    elif a.cmd == "bars":
        print(bars_text(a.symbol, a.tf, paper.bars_view(a.symbol, a.n, a.tf, a.session)))
    elif a.cmd == "order":
        _out(paper.place_order(a.symbol, a.side, a.lots, a.sl, a.tp, a.why, rr_gate=True))
    elif a.cmd == "close":
        _out(paper.close_position(a.id, a.why, a.symbol, a.volume))
    elif a.cmd == "modify":
        _out(paper.modify_position(a.id, a.sl, a.tp, a.why, a.symbol))
    elif a.cmd == "widen":
        _out(paper.widen_stop(a.id, a.sl, a.why, a.symbol))
    elif a.cmd == "watch":
        _out(paper.set_watch(a.id, a.symbol, a.above, a.below, a.in_min, a.quiet, a.quiet_min, a.clear, a.why))
    elif a.cmd == "journal":
        _out(paper.journal(a.text))
    elif a.cmd == "risk-factor":
        levers = any(v is not None for v in (a.day_halt, a.giveback, a.max_positions))
        if a.set_pct is None and not levers:
            _out(paper.risk_factor())
        else:
            res = {}
            if a.set_pct is not None:
                res["risk_pct"] = paper.set_risk_factor(a.set_pct, a.why or "")
            if levers:
                res["levers"] = paper.set_risk_levers(a.why or "", a.day_halt, a.giveback, a.max_positions)
            _out(res)
    elif a.cmd == "goals":
        _out(paper.goals())
    elif a.cmd == "fitness":
        _out(paper.fitness())
    elif a.cmd == "ledger":
        rows = paper.visible_ledger()
        if a.types:
            keep = {x.strip() for x in a.types.split(",")}
            rows = [r for r in rows if r.get("type") in keep]
        elif not a.all:
            rows = [r for r in rows if r.get("type") not in LEDGER_NOISE]
        print("\n".join(json.dumps(ledger_brief(r), separators=(",", ":")) for r in rows[-a.n:]))
    elif a.cmd == "attempts":
        _out(paper.recent_attempts(a.n))
    elif a.cmd == "real-eval":
        _out(paper.real_eval_chances())
    elif a.cmd == "alert":
        if a.cancel is not None:
            _out(paper.cancel_alert(a.cancel, a.why or "cancelled"))
        elif not a.symbol:
            _out({"ok": False, "reason": "--symbol is required (or --cancel ID)"})
        else:
            _out(paper.set_alert(a.symbol, a.above, a.below, a.why, a.hours, a.worker, a.when, a.tf, a.bars,
                                 a.sl, a.tp, require_plan=True))
    elif a.cmd == "alerts":
        _out(paper.s.get("alerts") or [])
    return 0


if __name__ == "__main__":
    sys.exit(main())
