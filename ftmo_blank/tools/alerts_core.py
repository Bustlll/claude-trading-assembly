"""PRICE ALERTS, the logic both desks share (2026-10-18).

A worker that sees a setup FORMING writes the plan and the condition once -- "sell if a closed M15 bar trades
above 3327 and closes back below it" -- and code, not a model, watches every closed bar for free. When the
condition is met inside the trading window, the symbol's trader wakes ONCE with the plan. This is how the
intelligence of one look carries forward without polling: the worker decides what matters, code waits for it.

Training (tools/paper.py, on the replay's bars) and live (tools/live_alerts.py, on the broker's closed bars)
both call these functions, so the same alert fires on the same bar on both desks (tools/parity_check.py
info.alerts drives both with the same market). Conditions = the vocabulary's triggers:
  touch   T-TOUCH   an M5 bar trades at/through the level        (refused closer than 0.5 x ATR(M15): a poll)
  close   T-CLOSE   a closed tf bar closes beyond the level
  hold    T-HOLD    N consecutive closed tf bars close beyond it
  reject  T-REJECT  a closed tf bar trades through it and closes back on the original side
"""
from __future__ import annotations

MAX_ACTIVE = 3                       # alerts a desk may have waiting at once
WHEN = ("touch", "close", "hold", "reject")
TFS = ("M5", "M15", "H1")
TF_SECONDS = {"M5": 300, "M15": 900, "H1": 3600}
TOUCH_MIN_ATR = 0.5
HOURS = (0.5, 24.0)
DEFAULT_HOURS = 8.0


def atr(m15_bars: list[dict]) -> float:
    """Mean high-low range of the last 14 closed M15 bars."""
    bars = m15_bars[-14:]
    return sum(b["h"] - b["l"] for b in bars) / len(bars) if bars else 0.0


def check_new(symbol: str, above, below, why: str, ref, atr_m15: float, when: str, tf: str, nbars, hours,
              active: list[dict], plan_sl=None, plan_tp=None, require_plan: bool = False) -> tuple[str | None, dict]:
    """(refusal reason or None, the normalized alert fields). The desk checks the symbol and the clock itself.
    plan_sl / plan_tp (2026-10-18, operator: "the scouts put a target and stop like a mock-up, the traders put
    the actual ones they want"): the planner's mock stop and target as prices, on opposite sides of the level --
    they fix the plan's side, and they are what the day's pass grading replays if the alert is met and passed.
    The CLI requires them (require_plan); the trader that wakes sets its own stop and target at entry."""
    if (above is None) == (below is None):
        return "give exactly one of --above P or --below P", {}
    if len(str(why).strip()) < 10:
        return "a plan is required in --why (at least 10 characters): what to do at the level", {}
    if ref is None:
        return f"no price for {symbol} yet", {}
    direction, level = ("above", float(above)) if above is not None else ("below", float(below))
    if (direction == "above" and ref >= level) or (direction == "below" and ref <= level):
        return (f"{symbol} last close {ref} is already {direction} {level}: an alert waits for a move that has not "
                "happened yet; act now instead if the setup is there"), {}
    if when not in WHEN:
        return f"--when must be one of {WHEN}", {}
    tf = "M5" if when == "touch" else tf
    if tf not in TFS:
        return f"--tf must be one of {TFS}", {}
    nbars = max(1, min(6, int(nbars))) if when == "hold" else 1
    if when == "touch" and atr_m15 > 0 and abs(level - ref) < TOUCH_MIN_ATR * atr_m15:
        return (f"{level} is {abs(level - ref):.5g} from price ({ref}), under {TOUCH_MIN_ATR} x ATR(M15) "
                f"{atr_m15:.5g}: a touch that close is a poll, not a plan. Wait for the CONDITION instead: --when "
                "close|hold|reject --tf M15 (code checks each closed bar, you wake only when it is met)"), {}
    side = None
    if plan_sl is not None or plan_tp is not None or require_plan:
        if plan_sl is None or plan_tp is None:
            return ("an alert carries its plan's mock stop and target as prices: --sl P --tp P (on opposite sides of "
                    "the level; they set the plan's side and grade the plan if it is passed -- the trader that wakes "
                    "sets its own at entry)"), {}
        plan_sl, plan_tp = float(plan_sl), float(plan_tp)
        if plan_sl < level < plan_tp:
            side = "buy"
        elif plan_tp < level < plan_sl:
            side = "sell"
        else:
            return (f"--sl {plan_sl} and --tp {plan_tp} must sit on opposite sides of the level {level} (a buy: stop "
                    "below, target above; a sell: the reverse)"), {}
    if len(active) >= MAX_ACTIVE:
        return (f"{MAX_ACTIVE} alerts already wait (ids {[a['id'] for a in active]}); cancel one first "
                "(alert --cancel ID --why ...)"), {}
    hours = max(HOURS[0], min(HOURS[1], float(hours)))
    return None, {"symbol": symbol, "dir": direction, "level": level, "why": str(why).strip(), "when": when,
                  "tf": tf, "nbars": nbars, "held": 0, "ref": ref, "hours": hours,
                  "plan_side": side, "plan_sl": plan_sl, "plan_tp": plan_tp}


def text(a: dict) -> str:
    w, d, lvl, tf = a.get("when", "touch"), a["dir"], a["level"], a.get("tf", "M5")
    if w == "touch":
        return f"T-TOUCH: the first {a['symbol']} M5 bar trading {d} {lvl}"
    if w == "close":
        return f"T-CLOSE: the first closed {tf} bar closing {d} {lvl}"
    if w == "hold":
        return f"T-HOLD: {a.get('nbars', 2)} consecutive closed {tf} bars closing {d} {lvl}"
    return f"T-REJECT: a closed {tf} bar trading {d} {lvl} and closing back {'below' if d == 'above' else 'above'} it"


def step(a: dict, bar: dict) -> bool:
    """One closed bar (an M5 bar for touch, else the alert's tf bar): is the condition met on it? Updates the
    hold counter in `a`."""
    when, up, lvl = a.get("when", "touch"), a["dir"] == "above", a["level"]
    if when == "touch":
        return bar["h"] >= lvl if up else bar["l"] <= lvl
    beyond = bar["c"] > lvl if up else bar["c"] < lvl
    if when == "close":
        return beyond
    if when == "hold":
        a["held"] = a.get("held", 0) + 1 if beyond else 0
        return a["held"] >= a.get("nbars", 2)
    return (bar["h"] >= lvl and bar["c"] < lvl) if up else (bar["l"] <= lvl and bar["c"] > lvl)


def outside_window(a: dict) -> None:
    """A condition met outside the trading window does not fire; a hold starts counting again."""
    if a.get("when") == "hold":
        a["held"] = 0


HIT_KEYS = ("id", "symbol", "dir", "level", "why", "by", "when", "tf", "nbars", "set_utc", "ref", "expires_utc",
            "plan_side", "plan_sl", "plan_tp")
