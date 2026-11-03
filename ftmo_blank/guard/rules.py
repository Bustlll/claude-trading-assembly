"""GUARD: the hard limits, the instrument facts, and the goal verifiers.

The agent can READ this file but must not edit it. The driver records the sha256 of this file,
guard/instruments.json and guard/policy.py in guard/MANIFEST.json, and refuses to run a turn if any changes.

Instrument facts come from guard/instruments.json, which tools/build_specs.py writes from the DeltaForce
market data (MT5 facts). Commission (FTMO's $2.50/lot/side) and the spread snapshot are labelled as such.
Goals are computed from the ledger by code. The model cannot mark a goal passed.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

START_BALANCE = 10_000.0          # paper account, same size as a 10k FTMO challenge

# FTMO challenge rules, 2-step style. Source: field/ftmo_rules.md and field/ftmo_rules_gaps.md.
# 2026-10-18 (operator): training takes the LIVE evaluation's terms -- "we put the actual live targets, minimum
# 2 days traded, and then just to be within the limits of dd, no max limit of days". The demo's 10-day / +5% /
# 4-day window made every worker reason about pacing ("1/4 traded days, 6 left"), a pressure the live account
# never has; training that practises a different objective trades differently live.
CHALLENGE = {
    "profit_target": 0.10,        # +10% of start balance: the live evaluation's target
    "max_daily_loss": 0.05,       # 5% of start balance. FTMO measures from balance at the 00:00 CE(S)T reset.
    "max_total_loss": 0.10,       # 10% of start balance, static
    "min_trading_days": 2,        # days with at least one opened position (the live account's minimum)
    "deadline_business_days": None,  # NO deadline: an attempt runs until it passes or breaches
}
# How the replay's history is cut into segments: only the places an epoch restarts from and how the dashboard
# counts progress through the data. Since 2026-10-18 NOT a deadline (see CHALLENGE above).
SEGMENT_BUSINESS_DAYS = 10

# Approximation: the guard measures the daily limit from equity at the reset (includes floating P/L at that
# moment). FTMO measures from balance. Same when flat at midnight.
DAY_TZ = "Europe/Prague"          # FTMO daily reset 00:00 CE(S)T (settled: field/ftmo_rules_gaps.md)

# Feed stamps are the broker clock, encoded as a naive epoch (a wall-clock reading, not a true UTC
# instant). The broker clock = New York local time + 7 hours. This is SEASONAL, not fixed: +2h from
# true UTC in NY winter (EST), +3h in NY summer (EDT). Verified two ways: against HistData's own
# DST-aware build (research276/storm/histdata_build.py) and directly against this project's own MT5
# data -- bit-identical close prices at matching timestamps, checked in both a winter and a summer
# month. A single fixed +3h (the original assumption here) is wrong by 1 hour across winter months.
_NY = ZoneInfo("America/New_York")


def broker_to_utc(t_broker: int) -> datetime:
    """The true UTC instant of a broker-clock reading. See the note above: seasonal, not fixed."""
    guess = datetime.fromtimestamp(t_broker - 2 * 3600, tz=timezone.utc)  # close enough to tell the season
    offset_h = 3 if guess.astimezone(_NY).dst() else 2
    return datetime.fromtimestamp(t_broker - offset_h * 3600, tz=timezone.utc)

# Operator, 2026-10-20: "the only hard ones are the 5 percent max dd per day loss and the max 10 percent total dd --
# those cannot be changed or updated"; everything else is the risk worker's to set from evidence. So the per-trade
# ceiling is the daily line itself (the worst-case check refuses any order that could cross it anyway), and the
# assembly's own halts are levers (tools/live_broker.py risk levers) bounded only by these lines. Was 0.01 / 0.003.
MAX_RISK_PER_TRADE = 0.05         # = CHALLENGE max_daily_loss: no rail of our own above FTMO's
# The per-trade risk factor is TUNABLE within [RISK_FACTOR_MIN, MAX_RISK_PER_TRADE] by the "risk" kind
# (guard/policy.py), based on evidenced trade quality/consistency -- never outside that range, and never
# by any other kind: see effective_risk_pct() below and paper.py's set_risk_factor(). The guard fixes the
# RANGE; what the current value actually is lives in state (state/account.json's "risk_pct"), because it
# is meant to move over the life of the account, not be re-derived from a frozen file every time.
RISK_FACTOR_MIN = 0.001           # floor (was 0.003): the risk worker may go smaller too
OWN_DAY_HALT_MAX = 0.05           # the assembly's own day-loss halt may be set up to the FTMO daily line
OWN_GIVEBACK_MAX = 0.10           # its own giveback-from-peak halt, up to the FTMO total line
MAX_POSITIONS_CAP = 100           # its own concurrent-position cap may go up to this
# the own rails' starting values, both desks (live_broker levers, paper levers) until the risk worker moves them
OWN_DAY_HALT_DEFAULT = 0.015
OWN_GIVEBACK_DEFAULT = 0.03
MAX_POSITIONS_DEFAULT = 5
RISK_FACTOR_DEFAULT = 0.007       # starting point: the live deltaforce book's own ~0.7% max risk per trade
WARN_FRACTION = 0.6               # emit risk_warning when a loss reaches this share of a limit

# TRAINING ONLY, mechanical, not a decision any kind makes: every open position is force-closed when an attempt
# ends (a pass or a breach -- since 2026-10-18 there is no deadline), because the paper account resets --
# there is nothing to hold into the next one. This is a program-level switch, not a trading call, and
# it is why routing.py tells every trade_closed review whose reason is "attempt_end" to judge the ENTRY,
# never the exit. Flip to False for a live deployment, where there is no window to reset.
DEMO_AUTO_CLOSE_AT_WINDOW_END = True


def effective_risk_pct(state: dict) -> float:
    """The CURRENT per-trade risk fraction, clamped into the guard's own hard range regardless of what
    state/account.json says -- the range is enforced here, every time, not just when risk_pct was set."""
    return max(RISK_FACTOR_MIN, min(MAX_RISK_PER_TRADE, state.get("risk_pct", RISK_FACTOR_DEFAULT)))

# Cumulative pass rate thresholds (fraction of ALL attempts ever, across epochs) that each fire a
# self-review event once, the first time they are crossed (operator's rule).
PASS_RATE_MILESTONES = [0.50, 0.70, 0.80, 0.90, 0.95]

# The replay starts at the first step where at least this many symbols have bars. Earlier history is
# not traded (on this data only 6 symbols reach 2024, 10 reach 2025, most start in May 2026).
REPLAY_MIN_SYMBOLS = 10

# Compounding ladder: each rung is an equity return target vs START_BALANCE, passed at any bar close.
LADDER = [0.02, 0.04, 0.06, 0.08, 0.10]  # back on the +10% live target (2026-10-18; was 1..5 for the 5% demo)

_HERE = Path(__file__).resolve().parent
INSTRUMENTS = json.loads((_HERE / "instruments.json").read_text())


def spec(symbol: str) -> dict:
    if symbol not in INSTRUMENTS:
        raise KeyError(f"no instrument spec for {symbol}; replayable: {sorted(INSTRUMENTS)}")
    return INSTRUMENTS[symbol]


def daily_loss_limit_usd() -> float:
    return START_BALANCE * CHALLENGE["max_daily_loss"]


def total_loss_limit_usd() -> float:
    return START_BALANCE * CHALLENGE["max_total_loss"]


def demo_passed(final_equity: float, traded_days: int) -> bool:
    """Whether an attempt's own numbers clear TODAY's demo bar -- independent of whatever outcome
    string got stored in the ledger at the time it actually ended. The profit target has changed
    once already (10% -> 5%, operator's update): an attempt that ended "window_end" under the old
    rule can still clear the current one. Always recompute from the numbers, never trust the old
    label. See field/real_progression.md for what passing this is practice for."""
    return (final_equity >= START_BALANCE * (1 + CHALLENGE["profit_target"])
            and traded_days >= CHALLENGE["min_trading_days"])


def rungs_passed(equity: float) -> int:
    return sum(1 for r in LADDER if equity >= START_BALANCE * (1 + r))


def risk_usd(symbol: str, lots: float, entry: float, sl: float) -> float:
    """USD lost if the stop is hit, using the MT5 tick value of the symbol."""
    return abs(entry - sl) * lots * spec(symbol)["usd_per_price_unit_per_lot"]


def chart_r_multiple(side: str, entry, exit_px, sl):
    """Realised R on PRICE, against the trade's own stop distance. The same formula as
    deltaforce/dashboard/server.py::_chart_r_multiple (parity-tested in tests/test_paper.py).
    Gross of commission. None when the stop is missing or degenerate."""
    if entry is None or exit_px is None or sl is None:
        return None
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    move = (exit_px - entry) if side == "long" else (entry - exit_px)
    return round(move / risk, 3)


def goal_table(state: dict) -> list[dict]:
    """Every goal, its verdict, and the evidence the verdict was computed from."""
    eq = state["equity"]
    traded = len(state["traded_days"])
    covered = len(set(state["traded_days"]) & set(state.get("note_days", [])))
    rows = [{"goal": "P_field_read", "kind": "process", "passed": bool(state.get("field_read")),
             "evidence": "paper.py field was run" if state.get("field_read") else "not yet read"}]
    for k, r in enumerate(LADDER):
        target = START_BALANCE * (1 + r)
        rows.append({"goal": f"L{k}_equity_plus_{int(r * 100)}pct", "kind": "ladder",
                     "passed": eq >= target,
                     "evidence": f"equity {eq:.2f} vs target {target:.2f}"})
    rows.append({"goal": "P_journal_every_traded_day", "kind": "process",
                 "passed": traded > 0 and covered == traded,
                 "evidence": f"journal note on {covered}/{traded} traded days (each traded day needs its own note)"})
    passed = (eq >= START_BALANCE * (1 + CHALLENGE["profit_target"]) and traded >= CHALLENGE["min_trading_days"]
              and state.get("breached") is None)
    if CHALLENGE["deadline_business_days"]:
        wday = state.get("window_day", 0)
        rows.append({"goal": "DEMO_within_the_window", "kind": "final", "passed": passed,
                     "evidence": f"window day {wday}/{CHALLENGE['deadline_business_days']}; equity {eq:.2f} vs target "
                                 f"{START_BALANCE * (1 + CHALLENGE['profit_target']):.2f}"})
    rows.append({"goal": "CHALLENGE_pass", "kind": "final", "passed": passed,
                 "evidence": f"equity {eq:.2f} vs +{CHALLENGE['profit_target']:.0%} target "
                             f"{START_BALANCE * (1 + CHALLENGE['profit_target']):.2f}, traded days "
                             f"{traded}/{CHALLENGE['min_trading_days']}, breached={state.get('breached')}, no deadline"})
    return rows


def order_check(state: dict, order: dict, ref_price: float, open_risk_usd: float,
                pending_risk_usd: float) -> tuple[bool, str]:
    """Return (ok, reason). Every refusal is a reason the agent can read and learn from."""
    if state.get("breached"):
        return False, f"account breached ({state['breached']}); no new orders"
    try:
        sp = spec(order["symbol"])
    except KeyError as exc:
        return False, str(exc)
    lots = order["lots"]
    vmin, vstep = sp["volume_min"], sp["volume_step"]
    if lots < vmin - 1e-12 or abs(round(lots / vstep) * vstep - lots) > 1e-9:
        return False, f"lots {lots} must be >= {vmin} in steps of {vstep} for {order['symbol']}"
    sl, tp, side = order["sl"], order.get("tp"), order["side"]
    if sl is None:
        return False, "a stop loss is required on every order"
    if side == "buy" and not sl < ref_price:
        return False, f"buy stop {sl} must be below reference price {ref_price}"
    if side == "sell" and not sl > ref_price:
        return False, f"sell stop {sl} must be above reference price {ref_price}"
    if tp is not None and ((side == "buy" and not tp > ref_price) or (side == "sell" and not tp < ref_price)):
        return False, f"take profit {tp} is on the wrong side of {ref_price}"
    risk = risk_usd(order["symbol"], lots, ref_price, sl)
    cap_pct = effective_risk_pct(state)
    if risk > cap_pct * START_BALANCE + 1e-9:
        return False, f"stop risk ${risk:.2f} exceeds the current per-trade cap ${cap_pct * START_BALANCE:.2f} " \
                      f"({cap_pct * 100:.2f}% -- paper.py risk-factor shows who set it and why)"
    eq = state["equity"]
    day_used = state["day_start_equity"] - eq
    total_used = START_BALANCE - eq
    worst_day = day_used + open_risk_usd + pending_risk_usd + risk
    worst_total = total_used + open_risk_usd + pending_risk_usd + risk
    if worst_day >= daily_loss_limit_usd():
        return False, (f"if every stop hits, the day loses ${worst_day:.2f}, at or past the daily limit "
                       f"${daily_loss_limit_usd():.2f}")
    if worst_total >= total_loss_limit_usd():
        return False, (f"if every stop hits, the account loses ${worst_total:.2f}, at or past the total limit "
                       f"${total_loss_limit_usd():.2f}")
    return True, f"stop risk ${risk:.2f}; worst-case day ${worst_day:.2f}/{daily_loss_limit_usd():.0f}"


def modify_check(side: str, old_sl: float, new_sl: float | None) -> tuple[bool, str]:
    """A stop may move toward profit, never away from it (no widening)."""
    if new_sl is None:
        return True, "unchanged"
    if side == "buy" and new_sl < old_sl:
        return False, f"cannot widen a buy stop from {old_sl} down to {new_sl}"
    if side == "sell" and new_sl > old_sl:
        return False, f"cannot widen a sell stop from {old_sl} up to {new_sl}"
    return True, "ok"
