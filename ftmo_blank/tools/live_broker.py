"""LIVE BROKER: real MT5 connectivity, and every gate a real order must pass.

See manual/LIVE_TRADING_IDEA.md for the design this implements. This used to be a pure scaffold
(every gate real, the broker itself a stub that always refused) -- as of 2026-10-10, connected()
is a REAL probe against the same shared MT5 bridge every deltaForceFTMO runner uses
(pymt5linux, localhost:MT5_PORT). The operator deliberately shares that terminal rather than
running a second one: they swap which account is logged into it; this tool and every
deltaForceFTMO runner just read "whatever is logged in right now," so nothing here needs to
change when the account does.

  live_broker.py status                                armed? connected? what would refuse and why
  live_broker.py order --symbol S --side buy|sell --lots L --sl P [--tp P] --why "..."
  live_broker.py close --symbol S ID [--volume V] --why "..."   all of it, or V lots of it (a partial close)
  live_broker.py modify --symbol S ID [--sl P] [--tp P] --why "..."  stop toward profit only; target either way
  live_broker.py widen --symbol S ID --sl P --why "..."     a WIDER stop: the live_risk kind only, once per trade
  live_broker.py journal --text "..."                  one note for the live record (no broker call)
  live_broker.py risk-factor [--set PCT --why "..."]  the live per-trade risk lever (setting it: live_risk kind only)

Market READS (symbols, bars, closed trades) live in tools/live_market.py, which has no order verb.

Two independent switches must BOTH be true before anything here can place a real order:
  1. the dashboard master switch is LIVE (state/MODE) -- checked HERE too (2026-10-11), not only
     by driver.py choosing what to run: a call made while MODE says TRAINING or OFF refuses even
     if LIVE_ARMED exists, so no stray process can fill for real outside LIVE.
  2. state/LIVE_ARMED exists -- operator-only, no kind may create it (not in any guard allow list).
Even with both true, connected() can still be False (bridge down, nobody logged in) -- that is a
real-time probe now, not a permanent stub.

Money is measured in the ACCOUNT's own currency (2026-10-11): every risk number below comes from
MT5's own order_calc_profit(), which applies the tick value and the FX conversion for whatever
currency the logged-in account is in. guard/rules.py risk_usd() is USD-only; the shared terminal
is a EUR account, so USD risk compared against a EUR balance mixed units.

Deal history is queried on the BROKER clock (2026-10-11, probed on the bridge): history_deals_get's
date bounds are compared against broker-clock epochs (UTC+2/+3), not true UTC, so a window ending
at "now UTC" silently dropped the last 2-3 hours of deals. _deals_between() pads the query and
filters on each deal's true UTC instant instead.

Because the shared account can be ANY size (confirmed: it is currently a EUR 10k FTMO-Demo
trial, not fixed to any one number), the risk gate below does NOT reuse guard/rules.py's
order_check() unchanged -- that function anchors its daily/total loss limits to the module
constant R.START_BALANCE, this project's own fixed $10k demo-challenge size. _risk_check() here
re-derives the same worst-case shape against the REAL account's own balance, tracked per login
(see _account_anchor) so a different account swapped onto this terminal gets its own anchor
automatically, never this project's demo number."""
from __future__ import annotations
import argparse
import datetime as dt
import fcntl
import json
import math
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from guard import rules as R  # noqa: E402

MT5_LIB = os.path.expanduser("~/trainAI/computerVisionFXUI/pymt5linux")
if MT5_LIB not in sys.path:
    sys.path.insert(0, MT5_LIB)

LOCK_PATH = HOME / "state" / "live_broker.lock"
LEDGER_PATH = HOME / "state" / "live_ledger.jsonl"
DAY_STATE_PATH = HOME / "state" / "live_day.json"
ACCOUNTS_STATE_PATH = HOME / "state" / "live_accounts.json"
SOURCE_TAG = "ftmo_blank_live"  # this project's own identity on any order, never confused with a paper fill

# Own magic number -- distinct from deltaForceFTMO's shared default (313700) and its cobra_live.py
# override (990517), so a position from this tool is unmistakable in the broker's own list by
# magic alone, on top of the comment tag.
MAGIC = int(os.environ.get("FTMO_BLANK_MT5_MAGIC", "777042"))
# The SAME shared bridge every deltaForceFTMO runner connects to (port 8001 by default) -- not a
# second terminal. Whatever account is logged in there is what this trades; the operator changes
# accounts on that one terminal, not per-tool (manual/LIVE_TRADING_IDEA.md, confirmed with the
# operator before wiring this).
MT5_PORT = int(os.environ.get("FTMO_BLANK_MT5_PORT", "8001"))
DEVIATION_POINTS = int(os.environ.get("FTMO_BLANK_MT5_DEVIATION", "20"))
PEAK_STATE_PATH = HOME / "state" / "live_peak.json"
# deltaForceFTMO's shared live ledger: every runner appends one row per order there, and its dashboard
# joins open positions and closes to a source by ticket. Writing ours lets the assembly's trades show in
# the delta dashboard's LIVE/CLOSED TRADES under their own ASSEMBLY badge (operator, 2026-10-11).
DELTA_LEDGER_PATH = Path(os.environ.get("FTMO_BLANK_DELTA_LEDGER",
                                        str(Path.home()) + "/deltaForceFTMO/deltaforce/data/ledgers/live_trades.jsonl"))

# Individually tuned for this worker, deliberately NOT a copy of deltaForceFTMO's own book-wide
# levers (0.7% day-loss / 1.5% giveback) -- operator's explicit call, 2026-10-10: looser than the
# book so this worker's real behaviour can actually be observed, while the real FTMO 5%/10% hard
# ceiling in _risk_check() still governs the WHOLE account underneath both of these, so neither
# being wrong can blow the account -- it can only let this worker run further before stopping.
# Both are measured against THIS tool's own contribution only (deal history + open positions
# filtered by MAGIC), never blended with whatever else the shared account's other runners are
# doing -- "per itself", the operator's own words.
DAY_LOSS_HALT_PCT = float(os.environ.get("FTMO_BLANK_DAY_LOSS_HALT_PCT", str(R.OWN_DAY_HALT_DEFAULT)))
PEAK_GIVEBACK_HALT_PCT = float(os.environ.get("FTMO_BLANK_PEAK_GIVEBACK_HALT_PCT", str(R.OWN_GIVEBACK_DEFAULT)))
MAX_CONCURRENT_POSITIONS = int(os.environ.get("FTMO_BLANK_MAX_CONCURRENT_POSITIONS", str(R.MAX_POSITIONS_DEFAULT)))
# The LIVE per-trade risk lever (2026-10-11): its own value in state/live_risk.json, tuned only by
# guard/policy.py's "live_risk" kind (live_broker.py risk-factor --set), inside the SAME guard range
# the paper lever uses [R.RISK_FACTOR_MIN 0.3%, R.MAX_RISK_PER_TRADE 1%]. Until that worker first sets
# it, live trades at LIVE_RISK_INITIAL_PCT = 0.3% (operator, 2026-10-11: "risk is fine at 0.3, let
# the risk analysis worker decide"). Before this it read the PAPER lever (state/account.json), which
# was never set, so a 0.1% bootstrap was shown and the 0.3% floor silently applied instead.
LIVE_RISK_PATH = HOME / "state" / "live_risk.json"
LIVE_RISK_INITIAL_PCT = float(os.environ.get("FTMO_BLANK_LIVE_RISK_INITIAL_PCT", "0.003"))
# The Friday flatten cutoff tools/live_watchdog.py enforces (it reads this same constant). A new
# entry at or past it would be closed by the watchdog within one tight poll -- pure spread cost.
FRI_FLAT_HOUR_UTC = int(os.environ.get("FTMO_BLANK_FRI_FLAT_HOUR_UTC", "20"))
MODES = ("OFF", "TRAINING", "LIVE")
_NY = ZoneInfo("America/New_York")


def armed() -> bool:
    return (HOME / "state" / "LIVE_ARMED").exists()


def mode() -> str:
    """state/MODE, read the same way driver.py mode() reads it (absent or garbage = TRAINING)."""
    try:
        m = (HOME / "state" / "MODE").read_text().strip()
    except OSError:
        return "TRAINING"
    return m if m in MODES else "TRAINING"


def _now_utc() -> dt.datetime:
    """The one wall clock every function here reads (day, Prague reset, deal windows, the entry gate)
    -- a single seam, so tests can pin it and every date in a test agrees."""
    return dt.datetime.now(dt.timezone.utc)


def utc_to_broker_epoch(t: dt.datetime) -> int:
    """Inverse of guard/rules.py broker_to_utc(): the broker clock is New York + 7h (UTC+3 in NY
    summer, UTC+2 in NY winter), encoded as a naive epoch."""
    offset_h = 3 if t.astimezone(_NY).dst() else 2
    return int(t.timestamp()) + offset_h * 3600


def _deals_between(m, since: dt.datetime, until: dt.datetime | None = None) -> list:
    """Deals whose TRUE UTC time lies in [since, until]. The bridge compares the query bounds
    against broker-clock epochs (probed 2026-10-11: a bound one hour before a deal's raw epoch
    excluded it), so the query is padded by a day on both sides and the exact cut is made here on
    R.broker_to_utc(deal.time)."""
    until = until or _now_utc()
    pad = dt.timedelta(days=1)
    deals = m.history_deals_get(since - pad, until + pad)
    if deals is None:
        raise RuntimeError("history_deals_get() returned None")
    out = []
    for d in deals:
        t = R.broker_to_utc(int(d.time))
        if since <= t <= until:
            out.append(d)
    return out


def _entry_window_block(now: dt.datetime | None = None) -> str | None:
    now = now or _now_utc()
    if (now.weekday() == 4 and now.hour >= FRI_FLAT_HOUR_UTC) or now.weekday() == 5:
        return (f"past the Friday flatten cutoff ({FRI_FLAT_HOUR_UTC}:00 UTC Friday): "
                "tools/live_watchdog.py would close a new position within seconds -- no new entries "
                "until the market reopens Sunday night")
    return None


def _mt5_client():
    """One fresh connection per call -- this tool is a short-lived CLI subprocess (like paper.py),
    not a long-running service, so there is no handle worth keeping warm between invocations.
    Raises on any failure; every caller below treats that as a hard refuse (fail-safe), never a
    silent default. This is also the one seam tests monkeypatch to simulate "no broker" or a fake
    connected one, without ever touching a real bridge."""
    from pymt5linux import MetaTrader5
    m = MetaTrader5(host="localhost", port=MT5_PORT)
    # pool_size=0 (2026-10-12): pymt5linux's initialize() otherwise opens FOUR extra connections and
    # runs mt5.initialize() on each of them inside the SHARED bridge process -- five re-inits of the
    # one MT5 session every deltaForceFTMO runner reads, for every CLI call, loop tick and dashboard
    # probe made here. The pool only speeds up parallel bulk fetches, which a one-shot call never does.
    if not m.initialize(pool_size=0):
        raise RuntimeError(f"MT5 initialize() failed: {m.last_error()}")
    return m


def ensure_symbol(m, symbol: str, wait_s: float = 3.0) -> dict:
    """Make `symbol` usable: activate it in the terminal's Market Watch if it is not, then wait up to
    wait_s for a quote. A fresh FTMO account starts on the broker's default Market Watch, and an
    inactive symbol can return no quote / no bars (deltaForceFTMO tools/ensure_symbols.py, which does
    this for the delta book at boot -- not for this assembly). symbol_select is not a subscription and
    costs nothing. Any symbol the broker offers can be activated for READING; ordering stays limited to
    guard/rules.py INSTRUMENTS (the order gate refuses the rest)."""
    base = {"symbol": symbol, "tradable_here": symbol in R.INSTRUMENTS}
    try:
        si = m.symbol_info(symbol)
    except Exception as exc:
        return {**base, "ok": False, "status": "read_failed", "reason": f"symbol_info({symbol}) failed: {exc}"}
    if si is None:
        return {**base, "ok": False, "status": "not_at_broker",
                "reason": f"{symbol} is not offered by this broker (live_market.py symbols --find TEXT searches its list)"}
    status = "already_active"
    if not getattr(si, "visible", True):
        try:
            selected = bool(m.symbol_select(symbol, True))
        except Exception:
            selected = False
        if not selected:
            return {**base, "ok": False, "status": "select_failed",
                    "reason": f"the terminal refused to activate {symbol}: {_safe_last_error(m)}"}
        status = "activated"
    deadline = time.time() + max(0.0, wait_s)
    tick = None
    while True:
        try:
            tick = m.symbol_info_tick(symbol)
        except Exception:
            tick = None
        if tick is not None and (getattr(tick, "bid", 0) or getattr(tick, "ask", 0)):
            break
        if time.time() >= deadline:
            tick = None
            break
        time.sleep(0.25)
    if tick is None:
        return {**base, "ok": False, "status": "no_quote_yet",
                "reason": f"{symbol} is {status.replace('_', ' ')} but has no quote yet (market shut, or the "
                          "terminal is still loading it -- try again in a minute)"}
    quote_utc = R.broker_to_utc(int(tick.time)).strftime("%Y-%m-%d %H:%M:%S") if getattr(tick, "time", 0) else None
    return {**base, "ok": True, "status": status, "bid": float(tick.bid), "ask": float(tick.ask), "quote_utc": quote_utc}


def _tick(m, symbol: str):
    """symbol_info_tick, and if it comes back empty: activate the symbol, wait briefly, ask once more."""
    t = m.symbol_info_tick(symbol)
    if t is None:
        ensure_symbol(m, symbol)
        t = m.symbol_info_tick(symbol)
    return t


def connected() -> bool:
    """A REAL probe now, not a permanent stub: true only if the shared bridge answers and reports
    an account. Fails closed on any exception (bridge down, wrong port, nobody logged in) --
    never raises, so a caller can always treat False as "nothing will trade" with certainty."""
    try:
        m = _mt5_client()
        return m.account_info() is not None
    except Exception:
        return False


def status() -> dict:
    out = {"mode": mode(), "armed": armed(), "connected": connected(),
           "would_refuse": _refusal_reason_if_any() or _entry_window_block(),
           "source_tag": SOURCE_TAG, "magic": MAGIC, "mt5_port": MT5_PORT}
    if out["connected"]:
        try:
            m = _mt5_client()
            st = _account_state(m)
            out["account"] = {"login": st["login"], "currency": st["currency"],
                               "balance": round(st["balance"], 2), "equity": round(st["equity"], 2),
                               "day_start_equity": round(st["day_start_equity"], 2),
                               "initial_balance": round(st["initial_balance"], 2),
                               "risk_pct": st["risk_pct"]}
            out["open_positions"] = _open_positions(m)
        except Exception as exc:
            out["account_error"] = str(exc)
    return out


def _refusal_reason_if_any() -> str | None:
    if not armed():
        return "not armed: state/LIVE_ARMED does not exist (operator-only, no kind may create it)"
    if mode() != "LIVE":
        return f"mode is {mode()}, not LIVE: the dashboard master switch must select LIVE (state/MODE)"
    if not connected():
        return "no broker connected: MT5 bridge unreachable or nobody logged in (tools/live_broker.py connected())"
    return None


class _SingletonLock:
    """Only one live order-affecting call proceeds at a time. The engine's own single_worker()
    (selfcall/engine.py) already serialises every Claude call; this is the SAME discipline against
    the broker connection itself, independently -- two Claude calls racing to both believe they
    may place an order is a live-only risk the single-process paper replay never has."""

    def __enter__(self):
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(LOCK_PATH, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._fh.close()
            raise RuntimeError("another live order call is already in flight (singleton lock held)")
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


def _day_tz():
    return ZoneInfo(R.DAY_TZ)


def _today_str() -> str:
    return _now_utc().astimezone(_day_tz()).date().isoformat()


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _write_json_atomic(path: Path, obj) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, indent=1))
        os.replace(tmp, path)
    except Exception:
        pass  # a failed write here only costs a day/anchor re-snapshot, never correctness


def _day_start_equity(equity_now: float, balance_now: float | None = None, m=None) -> float:
    """The day's starting point at the FTMO reset (guard/rules.py DAY_TZ, Europe/Prague 00:00),
    tracked in its own small file because LIVE has no replay clock to hook a rollover into.
    On the first read of a new day it is the BALANCE AT MIDNIGHT, rebuilt from the broker's deal
    history (balance now minus every deal since the reset) -- FTMO's own measure, and correct no
    matter how late in the day the first call happens. Before 2026-10-11 it was simply the equity
    at the first call of the day, so a loss taken before the loop's first look was invisible to
    the daily limit. Falls back to equity_now only if the history cannot be read."""
    today = _today_str()
    rec = _read_json(DAY_STATE_PATH, {})
    if rec.get("day") != today:
        start = equity_now
        how = "equity at first read (deal history unavailable)"
        if m is not None and balance_now is not None:
            try:
                today_deals = _deals_between(m, _prague_day_start_utc())
                start = balance_now - sum(float(getattr(d, "profit", 0) or 0) + float(getattr(d, "commission", 0) or 0)
                                          + float(getattr(d, "swap", 0) or 0) + float(getattr(d, "fee", 0) or 0)
                                          for d in today_deals)
                how = "balance at the Prague reset, from deal history"
            except Exception:
                pass
        rec = {"day": today, "day_start_equity": start, "how": how}
        _write_json_atomic(DAY_STATE_PATH, rec)
    return float(rec.get("day_start_equity", equity_now))


def _account_anchor(login: int, balance_now: float) -> float:
    """The account's own starting balance -- the fixed point FTMO's daily AND total loss rules are
    both actually measured against (guard/rules.py's daily_loss_limit_usd/total_loss_limit_usd
    both multiply the SAME fixed START_BALANCE, never a moving 'current balance'). Tracked per
    LOGIN, not globally, because the operator swaps which account sits on this shared terminal: a
    newly-seen login gets its balance-right-now recorded as its anchor exactly once, and that
    anchor sticks for as long as this tool has seen that login -- no manual reset needed when the
    account changes."""
    recs = _read_json(ACCOUNTS_STATE_PATH, {})
    key = str(login)
    if key not in recs:
        recs[key] = {"initial_balance": balance_now,
                     "first_seen_utc": _now_utc().isoformat()}
        _write_json_atomic(ACCOUNTS_STATE_PATH, recs)
    return float(recs[key]["initial_balance"])


def _live_risk_pct() -> float:
    """The live per-trade risk fraction actually used, always inside the guard's hard range --
    the same clamp R.effective_risk_pct() applies to the paper lever, so the number shown is the
    number sized with."""
    rec = _read_json(LIVE_RISK_PATH, {})
    try:
        pct = float(rec.get("risk_pct", LIVE_RISK_INITIAL_PCT))
    except (TypeError, ValueError):
        pct = LIVE_RISK_INITIAL_PCT
    return max(R.RISK_FACTOR_MIN, min(R.MAX_RISK_PER_TRADE, pct))


# ---- the assembly's own rails, as levers (operator, 2026-10-20) ----------------------------------------------
# "let the risk worker own them, and be able to update them ... the only hard ones are the 5 percent max dd per day
# loss and the max 10 percent total dd". The day-loss halt, the giveback halt and the position cap used to be fixed
# here (env defaults below); now live_risk sets them (risk-factor --day-halt/--giveback/--max-positions), kept in
# state/live_risk.json next to risk_pct, bounded only by FTMO's own lines (guard/rules.py OWN_*_MAX, MAX_POSITIONS_CAP).
LEVERS = {"day_halt_pct": (DAY_LOSS_HALT_PCT, 0.001, R.OWN_DAY_HALT_MAX),
          "giveback_halt_pct": (PEAK_GIVEBACK_HALT_PCT, 0.001, R.OWN_GIVEBACK_MAX),
          "max_positions": (MAX_CONCURRENT_POSITIONS, 1, R.MAX_POSITIONS_CAP)}


def _lever(name: str):
    default, lo, hi = LEVERS[name]
    try:
        v = float(_read_json(LIVE_RISK_PATH, {}).get(name, default))
    except (TypeError, ValueError):
        v = default
    v = max(lo, min(hi, v))
    return int(v) if name == "max_positions" else v


def risk_factor() -> dict:
    """Read the live levers: per-trade risk and the assembly's own rails, the ranges they must stay in, the history."""
    rec = _read_json(LIVE_RISK_PATH, {})
    return {"risk_pct": _live_risk_pct(), "set_by": "live_risk" if "risk_pct" in rec else "initial default",
            "min": R.RISK_FACTOR_MIN, "max": R.MAX_RISK_PER_TRADE,
            "levers": {k: {"value": _lever(k), "min": lo, "max": hi, "set": k in rec}
                       for k, (_, lo, hi) in LEVERS.items()},
            "hard_lines": {"daily_loss": R.CHALLENGE["max_daily_loss"], "total_loss": R.CHALLENGE["max_total_loss"]},
            "history": rec.get("history", [])[-10:]}


def set_risk_levers(why: str, day_halt: float | None = None, giveback: float | None = None,
                    max_positions: int | None = None) -> dict:
    """Set the assembly's own rails (live_risk only). Refused outside the FTMO-bounded ranges; every change is in the
    file's history and the live ledger."""
    if len(why.strip()) < 10:
        return {"ok": False, "reason": "a reason is required (at least 10 characters) in --why"}
    want = {k: v for k, v in (("day_halt_pct", day_halt), ("giveback_halt_pct", giveback),
                              ("max_positions", max_positions)) if v is not None}
    if not want:
        return {"ok": False, "reason": "give --day-halt, --giveback and/or --max-positions"}
    for k, v in want.items():
        _, lo, hi = LEVERS[k]
        if not (lo - 1e-12 <= float(v) <= hi + 1e-12):
            return {"ok": False, "reason": f"{k} {v} is outside [{lo}, {hi}] (bounded by FTMO's hard lines)"}
    rec = _read_json(LIVE_RISK_PATH, {})
    entry = {"utc": _now_utc().strftime("%Y-%m-%d %H:%M:%S"), "why": why.strip(),
             **{k: {"old": _lever(k), "new": v} for k, v in want.items()}}
    for k, v in want.items():
        rec[k] = int(v) if k == "max_positions" else float(v)
    rec.setdefault("history", []).append(entry)
    _write_json_atomic(LIVE_RISK_PATH, rec)
    log("risk_levers_changed", **entry)
    return {"ok": True, **{k: _lever(k) for k in want}}


def set_risk_factor(pct: float, why: str) -> dict:
    """Change the live lever. Guard-enforced: refused outside [RISK_FACTOR_MIN, MAX_RISK_PER_TRADE]
    whoever asks; a reason is required; every change is kept in the file's history and logged to
    the live ledger. Only guard/policy.py's live_risk kind is granted this verb."""
    if len(why.strip()) < 10:
        return {"ok": False, "reason": "a reason is required (at least 10 characters) in --why"}
    if not (R.RISK_FACTOR_MIN - 1e-12 <= pct <= R.MAX_RISK_PER_TRADE + 1e-12):
        return {"ok": False, "reason": f"risk_pct {pct} is outside the guard's allowed range "
                                       f"[{R.RISK_FACTOR_MIN}, {R.MAX_RISK_PER_TRADE}]"}
    rec = _read_json(LIVE_RISK_PATH, {})
    old = _live_risk_pct()
    entry = {"utc": _now_utc().strftime("%Y-%m-%d %H:%M:%S"), "old": old, "new": pct, "why": why.strip()}
    rec["risk_pct"] = pct
    rec.setdefault("history", []).append(entry)
    _write_json_atomic(LIVE_RISK_PATH, rec)
    if _live_risk_pct() != pct:
        return {"ok": False, "reason": "the new value could not be written to state/live_risk.json"}
    log("risk_factor_changed", **entry)
    return {"ok": True, "old": old, "new": pct}


def _account_state(m) -> dict:
    """The ONE real account state every gate below checks -- balance/equity read fresh from the
    broker every call, never cached, so two calls a second apart can never disagree about which
    account or how much is in it. 'breached' is always None here on purpose: unlike the paper
    account there is no persistent lockout flag for an arbitrary real account, so every call just
    re-checks the worst-case thresholds fresh against the CURRENT numbers -- strictly more
    conservative than trusting a stale breach flag, never less."""
    ai = m.account_info()
    if ai is None:
        raise RuntimeError("account_info() returned None")
    equity = float(ai.equity)
    balance = float(ai.balance)
    login = int(ai.login)
    return {"equity": equity, "balance": balance, "day_start_equity": _day_start_equity(equity, balance, m),
            "initial_balance": _account_anchor(login, balance),
            "breached": None, "risk_pct": _live_risk_pct(),
            "login": login, "currency": ai.currency}


def _position_side(p) -> str:
    return "buy" if int(getattr(p, "type", 0)) == 0 else "sell"


def _open_positions(m) -> list[dict]:
    """ALL open positions on the account, not just this tool's own -- self-hedge and the risk
    gate must see the whole account (manual/LIVE_TRADING_IDEA.md section 3: checked account-wide,
    not per-kind), because this shared terminal also carries deltaForceFTMO's own live runners'
    positions."""
    raw = m.positions_get()
    if raw is None:
        raise RuntimeError("positions_get() returned None")
    out = []
    for p in raw:
        sl = float(getattr(p, "sl", 0) or 0) or None
        tp = float(getattr(p, "tp", 0) or 0) or None
        out.append({"ticket": int(p.ticket), "symbol": p.symbol, "side": _position_side(p),
                     "lots": float(p.volume), "entry": float(p.price_open), "sl": sl, "tp": tp,
                     "magic": int(getattr(p, "magic", 0) or 0), "comment": getattr(p, "comment", "")})
    return out


def _pending_orders(m) -> list[dict]:
    """Pending (not yet filled) stop/limit orders account-wide, each priced from its own order
    price (or the current tick if it has none) to its stop -- mirrors paper.py's pending_risk()."""
    raw = m.orders_get()
    if raw is None:
        raise RuntimeError("orders_get() returned None")
    out = []
    for o in raw:
        sl = float(getattr(o, "sl", 0) or 0)
        if not sl:
            continue
        sell_side = int(getattr(o, "type", 0)) in (1, 3, 5)  # SELL / SELL_LIMIT / SELL_STOP
        ref = float(getattr(o, "price_open", 0) or 0)
        if not ref:
            tick = m.symbol_info_tick(o.symbol)
            if tick is None:
                raise RuntimeError(f"symbol_info_tick({o.symbol}) returned None")
            ref = float(tick.bid if sell_side else tick.ask)
        lots = float(getattr(o, "volume_current", 0) or getattr(o, "volume_initial", 0) or 0)
        if lots <= 0:
            continue
        out.append({"symbol": o.symbol, "side": "sell" if sell_side else "buy", "lots": lots, "sl": sl, "ref": ref})
    return out


def _loss_ccy(m, symbol: str, side: str, lots: float, from_price: float, to_price: float) -> float:
    """What moving from from_price to to_price would LOSE on this side and size, in the account's
    own currency -- MT5's order_calc_profit does the tick value and FX conversion. A move that
    would gain (a stop already past entry in profit) costs 0. Raises if the broker cannot price
    it: every caller treats that as a refusal (fail-closed)."""
    otype = m.ORDER_TYPE_BUY if side == "buy" else m.ORDER_TYPE_SELL
    pnl = m.order_calc_profit(otype, symbol, float(lots), float(from_price), float(to_price))
    if pnl is None:
        raise RuntimeError(f"order_calc_profit({symbol}) returned None")
    return max(0.0, -float(pnl))


def _open_risk_ccy(m, positions: list[dict]) -> float:
    """Account-wide: what every stopped position still loses if its stop hits FROM THE CURRENT
    PRICE (bid for a long's exit, ask for a short's). Measured from the current price, not entry,
    because the day/total 'used' numbers already include each position's floating P&L -- entry to
    stop would count a floating loss twice and a floating gain as cover it is not.
    Positions without a stop are skipped (unboundable), the same convention as paper.py."""
    ticks: dict = {}
    total = 0.0
    for p in positions:
        if not p.get("sl"):
            continue
        sym = p["symbol"]
        if sym not in ticks:
            t = m.symbol_info_tick(sym)
            if t is None:
                raise RuntimeError(f"symbol_info_tick({sym}) returned None")
            ticks[sym] = t
        mark = float(ticks[sym].bid if p["side"] == "buy" else ticks[sym].ask)
        total += _loss_ccy(m, sym, p["side"], p["lots"], mark, p["sl"])
    return total


def _pending_risk_ccy(m, pending: list[dict]) -> float:
    return sum(_loss_ccy(m, o["symbol"], o["side"], o["lots"], o["ref"], o["sl"]) for o in pending)


def _volume_rules(m, symbol: str) -> tuple[float, float]:
    """volume_min / volume_step from the broker's own symbol_info, guard/instruments.json only as
    a fallback (the live symbol is the authority on what it will accept)."""
    sp = R.spec(symbol)
    try:
        si = m.symbol_info(symbol)
    except Exception:
        si = None
    vmin = float(getattr(si, "volume_min", 0) or 0) or sp["volume_min"]
    vstep = float(getattr(si, "volume_step", 0) or 0) or sp["volume_step"]
    return vmin, vstep


def _self_hedge_ok(symbol: str, side: str, open_positions: list[dict]) -> tuple[bool, str]:
    """No opposite-side position in the same symbol, account-wide -- checked against every open
    position regardless of which worker opened it. This is the one real-only risk the per-symbol
    traders' own separation (guard/policy.py SYMBOL_TRADERS) does not remove by itself: two
    different kinds could still each want the same symbol in opposite directions at once."""
    opposite = "sell" if side == "buy" else "buy"
    clash = [p for p in open_positions if p.get("symbol") == symbol and p.get("side") == opposite]
    if clash:
        return False, f"self-hedge: {len(clash)} open {opposite} position(s) already in {symbol}"
    return True, "ok"


def _own_positions_raw(m) -> list:
    raw = m.positions_get()
    if raw is None:
        raise RuntimeError("positions_get() returned None")
    return [p for p in raw if int(getattr(p, "magic", 0) or 0) == MAGIC]


def _own_realized_pnl_usd(m, since: dt.datetime) -> float:
    """Realized P&L of ONLY this tool's own trades (filtered by MAGIC) since `since`, read from
    the broker's own deal history -- NOT reconstructed from this tool's own call log, because
    most closes happen at the broker itself (a stop or target hit), never through
    close_position()."""
    deals = _deals_between(m, since)
    return sum(float(d.profit) + float(d.commission) + float(d.swap) + float(getattr(d, "fee", 0) or 0)
               for d in deals if int(getattr(d, "magic", 0) or 0) == MAGIC)


def _own_floating_pnl_usd(m) -> float:
    return sum(float(getattr(p, "profit", 0) or 0) for p in _own_positions_raw(m))


def _prague_day_start_utc() -> dt.datetime:
    today = _now_utc().astimezone(_day_tz()).replace(hour=0, minute=0, second=0, microsecond=0)
    return today.astimezone(dt.timezone.utc)


def _own_day_pnl_usd(m) -> float:
    return _own_realized_pnl_usd(m, _prague_day_start_utc()) + _own_floating_pnl_usd(m)


def _own_equity_now(m, login: int, anchor: float) -> float:
    """anchor + this tool's own all-time realized P&L (since its anchor was first recorded, see
    _account_anchor) + its own current floating P&L -- an equity figure isolated to THIS tool's
    contribution, for the peak-giveback halt, never blended with the rest of the shared account."""
    recs = _read_json(ACCOUNTS_STATE_PATH, {})
    first_seen = recs.get(str(login), {}).get("first_seen_utc")
    since = (dt.datetime.fromisoformat(first_seen) if first_seen
             else _now_utc() - dt.timedelta(days=3650))
    return anchor + _own_realized_pnl_usd(m, since) + _own_floating_pnl_usd(m)


def _own_peak_equity(login: int, own_equity_now: float) -> float:
    """Persists on the FIRST sighting too, not only on a new high -- the bug an earlier version
    of this had: defaulting an unseen login's peak to 'own_equity_now itself' made 'is it higher
    than the default' trivially false forever, so nothing was ever written and the giveback halt
    was a silent no-op from the very first call. `key not in recs` must write unconditionally."""
    recs = _read_json(PEAK_STATE_PATH, {})
    key = str(login)
    if key not in recs or own_equity_now > float(recs[key]["peak"]):
        recs[key] = {"peak": own_equity_now, "updated_utc": _now_utc().isoformat()}
        _write_json_atomic(PEAK_STATE_PATH, recs)
        return own_equity_now
    return float(recs[key]["peak"])


def _max_concurrent_block(m) -> str | None:
    n = len(_own_positions_raw(m))
    cap = _lever("max_positions")
    if n >= cap:
        return f"max concurrent positions: already {n} open (limit {cap}, a live_risk lever)"
    return None


def _safety_halt_block(m, state: dict) -> str | None:
    """ftmo_blank's OWN day-loss and peak-giveback halts plus the max-concurrent cap -- all three
    individually tuned for this worker (see the constants above), all measured against only this
    tool's own contribution. The real FTMO 5%/10% hard ceiling in _risk_check() still governs the
    whole shared account underneath this regardless of whether these numbers are right."""
    login = state["login"]
    anchor = state["initial_balance"]
    day_pnl = _own_day_pnl_usd(m)
    day_halt = _lever("day_halt_pct")
    if day_pnl < 0 and abs(day_pnl) >= day_halt * anchor - 1e-9:
        return (f"day-loss halt: this worker's own day P&L ${day_pnl:.2f} is past the "
                f"{day_halt * 100:.1f}% halt (${day_halt * anchor:.2f} of ${anchor:.2f}; a live_risk lever)")
    own_equity = _own_equity_now(m, login, anchor)
    peak = _own_peak_equity(login, own_equity)
    giveback = peak - own_equity
    gb = _lever("giveback_halt_pct")
    if giveback >= gb * anchor - 1e-9:
        return (f"peak-giveback halt: ${giveback:.2f} given back from this worker's own peak "
                f"${peak:.2f} (>= {gb * 100:.1f}% of ${anchor:.2f}; a live_risk lever)")
    return _max_concurrent_block(m)


def _risk_check(state: dict, symbol: str, side: str, lots: float, sl: float | None, tp: float | None,
                ref_price: float, open_risk: float, pending_risk: float, risk: float | None,
                vmin: float, vstep: float) -> tuple[bool, str]:
    """Same shape as guard/rules.py's order_check(), re-anchored to the REAL account (see
    _account_state's own note on why that function cannot be reused unchanged here). Every money
    figure is in the account currency: risk/open_risk/pending_risk come from _loss_ccy() (MT5's
    own pricing), the limits from the account's own balance. `risk` is None only when the stop
    was missing or on the wrong side, both refused below before it would be used."""
    cur = state.get("currency", "")
    if state.get("breached"):
        return False, f"account breached ({state['breached']}); no new orders"
    if symbol not in R.INSTRUMENTS:
        return False, f"no instrument spec for {symbol}; tradable: {sorted(R.INSTRUMENTS)}"
    if lots < vmin - 1e-12 or abs(round(lots / vstep) * vstep - lots) > 1e-9:
        return False, f"lots {lots} must be >= {vmin} in steps of {vstep} for {symbol}"
    if sl is None:
        return False, "a stop loss is required on every order"
    if side == "buy" and not sl < ref_price:
        return False, f"buy stop {sl} must be below reference price {ref_price}"
    if side == "sell" and not sl > ref_price:
        return False, f"sell stop {sl} must be above reference price {ref_price}"
    if tp is not None and ((side == "buy" and not tp > ref_price) or (side == "sell" and not tp < ref_price)):
        return False, f"take profit {tp} is on the wrong side of {ref_price}"
    if risk is None:
        return False, "stop risk could not be priced"
    balance = state["balance"]
    cap_pct = max(R.RISK_FACTOR_MIN, min(R.MAX_RISK_PER_TRADE, state.get("risk_pct", R.RISK_FACTOR_DEFAULT)))
    if risk > cap_pct * balance + 1e-9:
        return False, (f"stop risk {risk:.2f} {cur} exceeds the current per-trade cap {cap_pct * balance:.2f} {cur} "
                        f"({cap_pct * 100:.2f}% of real balance {balance:.2f} {cur})")
    eq = state["equity"]
    anchor = state["initial_balance"]
    day_used = state["day_start_equity"] - eq
    total_used = anchor - eq
    daily_limit = anchor * R.CHALLENGE["max_daily_loss"]
    total_limit = anchor * R.CHALLENGE["max_total_loss"]
    worst_day = day_used + open_risk + pending_risk + risk
    worst_total = total_used + open_risk + pending_risk + risk
    if worst_day >= daily_limit:
        return False, (f"if every stop hits, the day loses {worst_day:.2f} {cur}, at or past the daily limit "
                        f"{daily_limit:.2f} {cur}")
    if worst_total >= total_limit:
        return False, (f"if every stop hits, the account loses {worst_total:.2f} {cur}, at or past the total "
                        f"limit {total_limit:.2f} {cur}")
    return True, f"stop risk {risk:.2f} {cur}; worst-case day {worst_day:.2f}/{daily_limit:.0f} {cur}"


def log(event: str, **data):
    """One ledger row. logged_utc (2026-10-12) stamps EVERY row with when it was written -- refusals,
    guard errors and modifies carried no time at all, so the live dashboard could not place them on its
    feed. A separate key on purpose: order_placed's own `utc` is what reconcile() reads, left untouched."""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"type": event, "source": SOURCE_TAG, "logged_utc": _now_utc().strftime("%Y-%m-%d %H:%M:%S"), **data}
    with open(LEDGER_PATH, "a") as f:
        f.write(json.dumps(row, default=float) + "\n")


def _filling_mode(m, si):
    fm = int(getattr(si, "filling_mode", 0) or 0)
    if fm & 1:
        return m.ORDER_FILLING_FOK
    if fm & 2:
        return m.ORDER_FILLING_IOC
    return m.ORDER_FILLING_RETURN


def _retcode_ok(m, result) -> bool:
    success = {m.TRADE_RETCODE_DONE}
    for name in ("TRADE_RETCODE_PLACED", "TRADE_RETCODE_DONE_PARTIAL"):
        v = getattr(m, name, None)
        if v is not None:
            success.add(v)
    return int(getattr(result, "retcode", -1)) in success


def _send_market_order(m, symbol: str, side: str, lots: float, sl: float, tp: float | None) -> dict:
    """Mirrors deltaForceFTMO's own mt5_forex_broker.place_order request shape (TRADE_ACTION_DEAL,
    filling mode read from the symbol's own bitmask, GTC, this tool's OWN magic/comment).
    order_send is NON-IDEMPOTENT (the lesson deltaForceFTMO's 2026-05-08 audit paid for): on any
    exception talking to the bridge this does NOT retry -- the outcome is ambiguous (the order may
    have reached the server before the response was lost), so it is reported as a failure and left
    for reconciliation, never auto-resent."""
    try:
        m.symbol_select(symbol, True)
    except Exception:
        pass
    si = m.symbol_info(symbol)
    if si is None:
        return {"ok": False, "reason": f"symbol_info({symbol}) returned None"}
    tick = _tick(m, symbol)
    if tick is None:
        return {"ok": False, "reason": f"symbol_info_tick({symbol}) returned None"}
    price = float(tick.ask if side == "buy" else tick.bid)
    request = {"action": m.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(lots),
               "type": m.ORDER_TYPE_BUY if side == "buy" else m.ORDER_TYPE_SELL,
               "price": price, "sl": float(sl), "deviation": DEVIATION_POINTS, "magic": MAGIC,
               "comment": SOURCE_TAG[:31], "type_time": m.ORDER_TIME_GTC,
               "type_filling": _filling_mode(m, si)}
    if tp is not None:
        request["tp"] = float(tp)
    try:
        result = m.order_send(request)
    except Exception as exc:
        last_err = _safe_last_error(m)
        return {"ok": False, "reason": f"order_send raised at the bridge (NOT retried): {exc}; last_error={last_err}"}
    if result is None:
        last_err = _safe_last_error(m)
        return {"ok": False, "reason": f"order_send returned None; last_error={last_err}"}
    if not _retcode_ok(m, result):
        return {"ok": False, "reason": f"broker refused: retcode={getattr(result, 'retcode', None)} "
                                        f"comment={getattr(result, 'comment', '')}"}
    return {"ok": True, "ticket": int(getattr(result, "order", 0) or getattr(result, "deal", 0) or 0),
            "lots_placed": float(getattr(result, "volume", lots)), "price": float(getattr(result, "price", price)),
            "retcode": int(getattr(result, "retcode"))}


def _safe_last_error(m):
    try:
        return m.last_error()
    except Exception:
        return None


# The per-symbol valuation table deltaForceFTMO's runners gate on (deltaforce/live/account_risk.py):
# marlin_specs.json, then its valuation-only extras -- the first file wins, as there. Shown NEXT TO the
# broker's own pricing whenever a worker sizes an order (operator, 2026-10-12, after the first live trade
# was sized 10x too small by mental math: "0.04 lots ~ 40 barrels" when one USOIL lot is 100 barrels).
RUNNER_SPEC_PATHS = [
    Path(os.environ.get("FTMO_BLANK_RUNNER_SPECS",
                        str(Path.home()) + "/deltaForceFTMO/deltaforce/jane04/tools/marlin_specs.json")),
    Path(os.environ.get("FTMO_BLANK_RUNNER_EXTRA_SPECS",
                        str(Path.home()) + "/deltaForceFTMO/deltaforce/live/account_risk_extra_specs.json")),
]
class SizingError(RuntimeError):
    """--risk-pct could not be turned into lots (bad value, wrong-side stop, stop too wide)."""


# An order using less than this share of the per-trade cap gets a note in its reply: almost always a
# lot-size slip, not a deliberate choice (a deliberate small trade can ignore it).
SMALL_ORDER_CAP_PCT = 25.0


def runner_table_uppl(symbol: str) -> float | None:
    """USD per 1.0 price move per lot in the runners' own table, or None when they have no entry
    (account_risk then prices the position at its pessimistic fallback)."""
    for path in RUNNER_SPEC_PATHS:
        sp = (_read_json(path, {}) or {}).get(symbol)
        if isinstance(sp, dict) and sp.get("usd_per_price_per_lot"):
            return float(sp["usd_per_price_per_lot"])
    return None


def _cap_pct(risk_pct: float) -> float:
    """The per-trade cap as a FRACTION of balance -- exactly the clamp _risk_check applies."""
    return max(R.RISK_FACTOR_MIN, min(R.MAX_RISK_PER_TRADE, risk_pct))


def _lots_for(risk_target: float, per_lot: float, vstep: float) -> float:
    """The largest lot size, in steps of vstep, whose stop risk stays within risk_target."""
    if per_lot <= 0 or risk_target <= 0:
        return 0.0
    n = int(risk_target / per_lot / vstep + 1e-9)
    return round(n * vstep, 8)


def size(symbol: str, side: str, sl: float, lots: float | None = None, risk_pct: float | None = None,
         m=None) -> dict:
    """The sizing table for one order; places nothing, needs neither armed nor LIVE. Everything a
    worker needs to choose lots without mental math: the broker's own price of the stop for one lot
    (order_calc_profit, account currency), the contract, the per-trade cap, the most lots that fit it,
    the runners' table for the same symbol -- and, given --lots or --risk-pct, that order's real risk.
    risk_pct is in PERCENT of balance (0.3 = 0.3%), as workers say it."""
    if side not in ("buy", "sell"):
        return {"ok": False, "reason": "side must be buy or sell"}
    m = m or _mt5_client()
    state = _account_state(m)
    tick = _tick(m, symbol)
    if tick is None:
        return {"ok": False, "reason": f"no quote for {symbol}"}
    ref = float(tick.ask if side == "buy" else tick.bid)
    if (side == "buy" and not sl < ref) or (side == "sell" and not sl > ref):
        return {"ok": False, "reason": f"{side} stop {sl} is on the wrong side of the current price {ref}"}
    vmin, vstep = _volume_rules(m, symbol) if symbol in R.INSTRUMENTS else (0.01, 0.01)
    per_lot = _loss_ccy(m, symbol, side, 1.0, ref, sl)
    cur, bal = state["currency"], state["balance"]
    cap_frac = _cap_pct(state["risk_pct"])
    cap = cap_frac * bal
    max_lots = _lots_for(cap, per_lot, vstep)
    si = m.symbol_info(symbol)
    tv, ts = float(getattr(si, "trade_tick_value", 0) or 0), float(getattr(si, "trade_tick_size", 0) or 0)
    uppl = runner_table_uppl(symbol)
    dist = abs(ref - sl)
    out = {"ok": True, "symbol": symbol, "side": side, "tradable_here": symbol in R.INSTRUMENTS,
           "price_now": ref, "sl": sl, "stop_distance": round(dist, 6), "currency": cur, "balance": round(bal, 2),
           "per_trade_cap": round(cap, 2), "per_trade_cap_pct": round(cap_frac * 100, 3),
           "broker": {"contract_size": float(getattr(si, "trade_contract_size", 0) or 0) or None,
                      f"{cur}_per_1.0_price_move_per_lot": round(tv / ts, 4) if ts else None,
                      f"stop_risk_per_lot_{cur}": round(per_lot, 2)},
           "runner_table": ({"usd_per_1.0_price_move_per_lot": uppl, "stop_risk_per_lot_usd": round(uppl * dist, 2)}
                            if uppl else "not in the runners' table: their risk gate would price it at its "
                                         "pessimistic fallback"),
           "volume_min": vmin, "volume_step": vstep,
           "max_lots_within_cap": max_lots if max_lots >= vmin - 1e-12 else 0.0}
    if out["max_lots_within_cap"] == 0.0:
        out["note"] = (f"even {vmin} lots risks {per_lot * vmin:.2f} {cur}, past the {cap:.2f} {cur} cap: "
                       "the stop is too wide for this account at this lever")
    if risk_pct is not None:
        if not 0 < risk_pct <= R.MAX_RISK_PER_TRADE * 100:
            return {"ok": False, "reason": f"--risk-pct is in PERCENT of balance, 0 < it <= "
                                           f"{R.MAX_RISK_PER_TRADE * 100:g} (0.3 means 0.3%)"}
        target = min(risk_pct / 100.0, cap_frac) * bal
        out["lots_for_risk_pct"] = _lots_for(target, per_lot, vstep)
        lots = lots if lots is not None else out["lots_for_risk_pct"]
    if lots is not None:
        risk = per_lot * lots
        out.update({"lots": lots, "risk": round(risk, 2), "risk_pct_of_balance": round(100 * risk / bal, 3),
                    "cap_used_pct": round(100 * risk / cap, 1) if cap else None,
                    "fits_cap": risk <= cap + 1e-9 and lots >= vmin - 1e-12})
    return out


def place_order(symbol: str, side: str, lots: float | None, sl: float, tp: float | None, why: str,
                open_positions: list[dict] | None = None, risk_pct: float | None = None) -> dict:
    """Gate order: rationale, then armed/connected (cheap -- no point touching the broker at all
    if either is false), then the real account read (breach), then this worker's OWN day-loss /
    peak-giveback / max-concurrent halts (_safety_halt_block), then self-hedge against the real
    account-wide position list, then the real worst-case risk check (the account-wide FTMO hard
    ceiling), then the singleton lock and the actual send. `open_positions`, if given, is used
    as-is (this is how tests exercise the self-hedge gate without a real broker); the real CLI
    path always leaves it None and this reads the account itself."""
    if len(why.strip()) < 10:
        return {"ok": False, "reason": "a rationale is required (at least 10 characters) in --why"}
    refusal = _refusal_reason_if_any() or _entry_window_block()
    if refusal:
        log("order_refused", symbol=symbol, side=side, lots=lots, reason=refusal)
        return {"ok": False, "reason": refusal}
    try:
        m = _mt5_client()
        state = _account_state(m)
    except Exception as exc:
        reason = f"account state unavailable (fail-closed): {exc}"
        log("order_refused", symbol=symbol, side=side, lots=lots, reason=reason)
        return {"ok": False, "reason": reason}
    if state.get("breached"):
        reason = f"account breached ({state['breached']}); no new orders"
        log("order_refused", symbol=symbol, side=side, lots=lots, reason=reason)
        return {"ok": False, "reason": reason}
    try:
        halt = _safety_halt_block(m, state)
    except Exception as exc:
        reason = f"safety-halt check failed (fail-closed): {exc}"
        log("order_refused", symbol=symbol, side=side, lots=lots, reason=reason)
        return {"ok": False, "reason": reason}
    if halt:
        log("order_refused", symbol=symbol, side=side, lots=lots, reason=halt)
        return {"ok": False, "reason": halt}
    if open_positions is None:
        try:
            open_positions = _open_positions(m)
        except Exception as exc:
            reason = f"positions read failed (fail-closed): {exc}"
            log("order_refused", symbol=symbol, side=side, lots=lots, reason=reason)
            return {"ok": False, "reason": reason}
    hedge_ok, hedge_reason = _self_hedge_ok(symbol, side, open_positions)
    if not hedge_ok:
        log("order_refused", symbol=symbol, side=side, lots=lots, reason=hedge_reason)
        return {"ok": False, "reason": hedge_reason}
    try:
        with _SingletonLock():
            try:
                m.symbol_select(symbol, True)
                tick = _tick(m, symbol)
                if tick is None:
                    raise RuntimeError(f"symbol_info_tick({symbol}) returned None")
                ref_price = float(tick.ask if side == "buy" else tick.bid)
                vmin, vstep = _volume_rules(m, symbol) if symbol in R.INSTRUMENTS else (0.01, 0.01)
                if lots is None:
                    # --risk-pct (PERCENT of balance): lots from the broker's own price of this stop
                    if risk_pct is None or not 0 < risk_pct <= R.MAX_RISK_PER_TRADE * 100:
                        raise SizingError("give --lots, or --risk-pct in PERCENT of balance "
                                           f"(0 < it <= {R.MAX_RISK_PER_TRADE * 100:g}; 0.3 means 0.3%)")
                    if sl is None or not ((side == "buy" and sl < ref_price) or (side == "sell" and sl > ref_price)):
                        raise SizingError(f"{side} stop {sl} is on the wrong side of {ref_price}")
                    per_lot = _loss_ccy(m, symbol, side, 1.0, ref_price, sl)
                    lots = _lots_for(min(risk_pct / 100.0, _cap_pct(state["risk_pct"])) * state["balance"],
                                     per_lot, vstep)
                    if lots < vmin - 1e-12:
                        raise SizingError(f"--risk-pct {risk_pct} buys less than the minimum {vmin} lots with this "
                                           f"stop ({per_lot * vmin:.2f} {state.get('currency', '')} at {vmin}): "
                                           "the stop is too wide for that risk")
                pending = _pending_orders(m)
                open_risk = _open_risk_ccy(m, open_positions)
                pending_risk = _pending_risk_ccy(m, pending)
                stop_ok = (symbol in R.INSTRUMENTS and sl is not None
                           and ((side == "buy" and sl < ref_price) or (side == "sell" and sl > ref_price)))
                risk = _loss_ccy(m, symbol, side, lots, ref_price, sl) if stop_ok else None
            except SizingError as exc:
                log("order_refused", symbol=symbol, side=side, lots=lots, risk_pct=risk_pct, reason=str(exc))
                return {"ok": False, "reason": str(exc)}
            except Exception as exc:
                reason = f"market data read failed (fail-closed): {exc}"
                log("order_refused", symbol=symbol, side=side, lots=lots, reason=reason)
                return {"ok": False, "reason": reason}
            ok, risk_reason = _risk_check(state, symbol, side, lots, sl, tp, ref_price, open_risk, pending_risk,
                                          risk, vmin, vstep)
            if not ok:
                log("order_refused", symbol=symbol, side=side, lots=lots, reason=risk_reason)
                return {"ok": False, "reason": risk_reason}
            consulted = _consulted_analysis(symbol)
            result = _send_market_order(m, symbol, side, lots, sl, tp)
            if result.get("ok"):
                result["entry"] = _filled_entry(m, result.get("ticket"), result.get("price"))
                result["consulted_analysis"] = consulted
                _delta_ledger_row(symbol, side, result, sl, tp)
                log("order_placed", symbol=symbol, side=side, lots=lots, sl=sl, tp=tp, why=why,
                    ref_price=ref_price, risk_ccy=round(risk or 0.0, 2), currency=state.get("currency"),
                    check=risk_reason, login=state.get("login"), utc=_now_utc().strftime("%Y-%m-%d %H:%M:%S"),
                    day=_today_str(), **result)
                # what this order REALLY risks, for the worker's reply only -- added after the ledger row,
                # whose own lots/currency/risk_ccy keys these would otherwise collide with
                cap = _cap_pct(state["risk_pct"]) * state["balance"]
                result.update({"lots": lots, "risk": round(risk or 0.0, 2), "currency": state.get("currency"),
                               "risk_pct_of_balance": round(100 * (risk or 0.0) / state["balance"], 3),
                               "per_trade_cap": round(cap, 2),
                               "cap_used_pct": round(100 * (risk or 0.0) / cap, 1) if cap else None})
                if cap and 100 * (risk or 0.0) / cap < SMALL_ORDER_CAP_PCT:
                    result["note"] = (f"this order risks only {result['cap_used_pct']}% of your per-trade cap "
                                      f"({result['risk']} of {result['per_trade_cap']} {state.get('currency', '')}). "
                                      "If that was not deliberate, check the lots: order ... --dry-run shows the "
                                      "broker's risk per lot")
            else:
                log("order_refused", symbol=symbol, side=side, lots=lots, sl=sl, tp=tp, why=why, **result)
            return result
    except RuntimeError as exc:
        log("order_refused", symbol=symbol, side=side, lots=lots, reason=str(exc))
        return {"ok": False, "reason": str(exc)}


def _delta_ledger_row(symbol: str, side: str, result: dict, sl: float, tp: float | None) -> None:
    """One row in deltaForceFTMO's live_trades.jsonl, in the shape its other runners write (LONG/SHORT,
    not the broker's BUY/SELL -- that ledger's readers compare against LONG/SHORT). Never raises: the
    order is already placed; a missing dashboard row must not turn into a refused order."""
    try:
        if not DELTA_LEDGER_PATH.parent.is_dir():
            return
        row = {"ts_fire": _now_utc().timestamp(), "symbol": symbol, "direction": "LONG" if side == "buy" else "SHORT",
               "lots": result.get("lots_placed"), "sl_price": sl, "tp_price": tp, "ticket": str(result.get("ticket")),
               "source": SOURCE_TAG, "comment": SOURCE_TAG, "tf": "live", "entry_price": result.get("entry")}
        with open(DELTA_LEDGER_PATH, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


def _consulted_analysis(symbol: str) -> bool:
    """Same tag paper.py puts on every order: did a specialist post a pre_trade finding for this
    symbol on the LIVE board today (Prague day), before this order? Checked by the tool itself,
    never self-reported, so a review can tell analysed entries from unanalysed ones."""
    try:
        import live_board
        return live_board.consulted(symbol, home=HOME)
    except Exception:
        return False


def _filled_entry(m, ticket, fallback):
    """The position's own open price, read back once after the fill (a market order can fill
    away from the price it was sent at). Falls back to the send result's price."""
    try:
        raw = m.positions_get(ticket=int(ticket)) if ticket else None
        if raw:
            return float(raw[0].price_open)
    except Exception:
        pass
    return fallback


# 2026-10-19 (operator: "make sure this is all JUST for assembly trades and not for the rest of them"): every verb
# that acts on an OPEN position checks the position's magic. Before this, close/modify only checked that the
# ticket's symbol matched -- a worker could have closed or moved a deltaForceFTMO runner's position by ticket.
def _own_position(m, order_id: int, symbol: str):
    """(position, None) for one of THIS assembly's own open positions, or (None, why not)."""
    raw = m.positions_get(ticket=order_id)
    if not raw:
        return None, f"position {order_id} not found"
    p = raw[0]
    if int(getattr(p, "magic", 0) or 0) != MAGIC:
        return None, (f"position {order_id} is not this assembly's (magic {int(getattr(p, 'magic', 0) or 0)}, ours "
                      f"{MAGIC}): another runner's position on the shared account is never touched")
    if p.symbol != symbol:
        return None, f"position {order_id} is {p.symbol}, not {symbol}"
    return p, None


def _rollover_hold(order_id: int) -> dict | None:
    """The watchdog's record while it holds this position's stop widened over the rollover (tools/live_watchdog.py)."""
    try:
        return (_read_json(HOME / "state" / "live_rollover.json", {}) or {}).get(str(int(order_id)))
    except Exception:
        return None


def _refused(event: str, symbol: str, order_id: int, reason: str) -> dict:
    log(event, symbol=symbol, id=order_id, reason=reason)
    return {"ok": False, "reason": reason}


def _send(m, request: dict, event: str, symbol: str, order_id: int):
    """order_send once (never retried: it is not idempotent); (result, None) or (None, refusal dict)."""
    try:
        result = m.order_send(request)
    except Exception as exc:
        return None, _refused(event, symbol, order_id, f"order_send raised at the bridge (NOT retried): {exc}")
    if result is None:
        return None, _refused(event, symbol, order_id, f"order_send returned None; last_error={_safe_last_error(m)}")
    if not _retcode_ok(m, result):
        return None, _refused(event, symbol, order_id, f"broker refused: retcode={getattr(result, 'retcode', None)}")
    return result, None


def close_position(symbol: str, order_id: int, why: str, volume: float | None = None) -> dict:
    """Close one of this assembly's positions: all of it, or `volume` lots of it (a partial close -- the same
    request deltaForceFTMO's ifvg/lizard runners have used live for months: a DEAL against the position ticket
    with a smaller volume). A split that would leave either part under the broker's minimum is refused, with
    the numbers, rather than silently turned into something else."""
    if len(why.strip()) < 10:
        return {"ok": False, "reason": "a reason is required in --why"}
    refusal = _refusal_reason_if_any()
    if refusal:
        return _refused("close_refused", symbol, order_id, refusal)
    try:
        m = _mt5_client()
    except Exception as exc:
        return _refused("close_refused", symbol, order_id, f"broker connection failed (fail-closed): {exc}")
    try:
        with _SingletonLock():
            p, why_not = _own_position(m, order_id, symbol)
            if p is None:
                return _refused("close_refused", symbol, order_id, why_not)
            buy = int(p.type) == 0
            have = float(p.volume)
            vol, partial = have, False
            if volume is not None:
                vmin, vstep = _volume_rules(m, p.symbol)
                v = round(math.floor(round(float(volume) / vstep, 6)) * vstep, 8)   # down to the step, never up
                if v <= 0:
                    return _refused("close_refused", symbol, order_id, f"--volume {volume} rounds to 0 (step {vstep})")
                if v < have - 1e-9:
                    rest = round(have - v, 8)
                    if v < vmin - 1e-12 or rest < vmin - 1e-12:
                        return _refused("close_refused", symbol, order_id, (
                            f"cannot split {have} lots into {v} closed + {rest} left: both parts must be >= the minimum "
                            f"{vmin} (step {vstep}). Close all of it, or move the stop instead"))
                    vol, partial = v, True
            tick = m.symbol_info_tick(p.symbol)
            if tick is None:
                return _refused("close_refused", symbol, order_id, f"symbol_info_tick({p.symbol}) returned None")
            si = m.symbol_info(p.symbol)
            request = {"action": m.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": float(vol),
                       "type": m.ORDER_TYPE_SELL if buy else m.ORDER_TYPE_BUY, "position": int(order_id),
                       "price": float(tick.bid if buy else tick.ask), "deviation": DEVIATION_POINTS,
                       "magic": MAGIC, "comment": f"{SOURCE_TAG[:20]}:{'part' if partial else 'close'}",
                       "type_time": m.ORDER_TIME_GTC, "type_filling": _filling_mode(m, si)}
            result, refused = _send(m, request, "close_refused", symbol, order_id)
            if refused:
                return refused
            row = {"symbol": symbol, "id": order_id, "why": why, "retcode": int(result.retcode),
                   "utc": _now_utc().strftime("%Y-%m-%d %H:%M:%S"), "volume": vol, "partial": partial}
            if partial:
                row["remaining"] = round(have - vol, 8)
            log("close_placed", **row)
            return {"ok": True, "retcode": int(result.retcode), "closed_lots": vol, "partial": partial,
                    **({"remaining_lots": row["remaining"]} if partial else {})}
    except RuntimeError as exc:
        return _refused("close_refused", symbol, order_id, str(exc))


def modify_position(symbol: str, order_id: int, new_sl: float | None, new_tp: float | None, why: str) -> dict:
    """Move this assembly's stop TOWARD PROFIT (never wider: guard/rules.py modify_check; widening is widen_stop,
    the risk worker's alone) and/or its target, either way (extend it to the next level, or bring it in front
    of one). Both must stay on their own side of the current price."""
    if len(why.strip()) < 10:
        return {"ok": False, "reason": "a reason is required in --why"}
    if new_sl is None and new_tp is None:
        return {"ok": False, "reason": "give --sl and/or --tp"}
    refusal = _refusal_reason_if_any()
    if refusal:
        return _refused("modify_refused", symbol, order_id, refusal)
    try:
        m = _mt5_client()
    except Exception as exc:
        return _refused("modify_refused", symbol, order_id, f"broker connection failed (fail-closed): {exc}")
    try:
        with _SingletonLock():
            p, why_not = _own_position(m, order_id, symbol)
            if p is None:
                return _refused("modify_refused", symbol, order_id, why_not)
            side = "buy" if int(p.type) == 0 else "sell"
            cur_sl = float(p.sl or 0)
            cur_tp = float(getattr(p, "tp", 0) or 0)
            tick = m.symbol_info_tick(p.symbol)
            if tick is None:
                return _refused("modify_refused", symbol, order_id, f"symbol_info_tick({p.symbol}) returned None")
            exit_px = float(tick.bid if side == "buy" else tick.ask)   # where this position would close now
            if new_sl is not None:
                hold = _rollover_hold(order_id)
                base = float(hold["orig"]) if hold and hold.get("orig") else cur_sl   # never "toward profit" from a cushion
                ok, check_reason = R.modify_check(side, base, new_sl)
                if not ok:
                    return _refused("modify_refused", symbol, order_id, check_reason + (
                        " (the risk worker alone may widen a stop: live_broker.py widen)" if "widen" in check_reason else ""))
                if (side == "buy" and new_sl >= exit_px) or (side == "sell" and new_sl <= exit_px):
                    return _refused("modify_refused", symbol, order_id,
                                    f"stop {new_sl} is on the wrong side of the current price {exit_px}: close instead")
            if new_tp is not None:
                if new_tp <= 0:
                    return _refused("modify_refused", symbol, order_id, "a target must be a price (> 0)")
                if (side == "buy" and new_tp <= exit_px) or (side == "sell" and new_tp >= exit_px):
                    return _refused("modify_refused", symbol, order_id,
                                    f"target {new_tp} is already behind the current price {exit_px}: close instead")
            sl_out = float(new_sl) if new_sl is not None else cur_sl
            tp_out = float(new_tp) if new_tp is not None else cur_tp
            request = {"action": m.TRADE_ACTION_SLTP, "symbol": p.symbol, "position": int(order_id),
                       "sl": sl_out, "tp": tp_out}
            result, refused = _send(m, request, "modify_refused", symbol, order_id)
            if refused:
                return refused
            log("modify_placed", symbol=symbol, id=order_id, old_sl=cur_sl, new_sl=sl_out, old_tp=cur_tp or None,
                new_tp=tp_out or None, why=why, retcode=int(result.retcode), utc=_now_utc().strftime("%Y-%m-%d %H:%M:%S"))
            return {"ok": True, "retcode": int(result.retcode), "sl": sl_out, "tp": tp_out or None}
    except RuntimeError as exc:
        return _refused("modify_refused", symbol, order_id, str(exc))


def modify_stop(symbol: str, order_id: int, new_sl: float, why: str) -> dict:
    """The stop-only form (kept for every caller written before targets could be moved)."""
    return modify_position(symbol, order_id, new_sl, None, why)


# Operator, 2026-10-19: "the widening of the stops can only be managed by the risk analyst". The guard's
# never-wider rule stays for every other kind; this verb is the one exception, granted to live_risk alone
# (guard/policy.py), and bounded here in code: once per trade, at most WIDEN_MAX_RISK_MULT x the trade's own
# risk at entry, never while the watchdog holds a rollover cushion, never past a halt, and only if the account
# could still take every open stop -- this one at its new level -- inside the FTMO daily and total limits.
WIDEN_MAX_RISK_MULT = float(os.environ.get("FTMO_BLANK_WIDEN_MAX_RISK_MULT", "2.0"))


def widen_stop(symbol: str, order_id: int, new_sl: float, why: str) -> dict:
    if len(why.strip()) < 20:
        return {"ok": False, "reason": "a widen needs its reason in --why (the level it must clear and why, >= 20 chars)"}
    refusal = _refusal_reason_if_any()
    if refusal:
        return _refused("widen_refused", symbol, order_id, refusal)
    try:
        m = _mt5_client()
    except Exception as exc:
        return _refused("widen_refused", symbol, order_id, f"broker connection failed (fail-closed): {exc}")
    try:
        with _SingletonLock():
            p, why_not = _own_position(m, order_id, symbol)
            if p is None:
                return _refused("widen_refused", symbol, order_id, why_not)
            if _rollover_hold(order_id):
                return _refused("widen_refused", symbol, order_id,
                                "the watchdog holds this stop widened over the rollover: widen after it restores it")
            if any(r.get("type") == "widen_placed" and str(r.get("id")) == str(order_id) for r in ledger_rows()):
                return _refused("widen_refused", symbol, order_id, "this position's stop was already widened once")
            side = "buy" if int(p.type) == 0 else "sell"
            cur_sl = float(p.sl or 0)
            if cur_sl <= 0:
                return _refused("widen_refused", symbol, order_id, "the position has no stop to widen")
            if (side == "buy" and not new_sl < cur_sl) or (side == "sell" and not new_sl > cur_sl):
                return _refused("widen_refused", symbol, order_id,
                                f"{new_sl} is not wider than the current stop {cur_sl}: that is a modify, not a widen")
            tick = m.symbol_info_tick(p.symbol)
            if tick is None:
                return _refused("widen_refused", symbol, order_id, f"symbol_info_tick({p.symbol}) returned None")
            exit_px = float(tick.bid if side == "buy" else tick.ask)
            if (side == "buy" and new_sl >= exit_px) or (side == "sell" and new_sl <= exit_px):
                return _refused("widen_refused", symbol, order_id, f"stop {new_sl} is on the wrong side of {exit_px}")
            entry, lots = float(p.price_open), float(p.volume)
            placed = next((r for r in ledger_rows() if r.get("type") == "order_placed"
                           and str(r.get("ticket")) == str(order_id)), None)
            risk0 = float((placed or {}).get("risk_ccy") or 0) or _loss_ccy(m, p.symbol, side, lots, entry, cur_sl)
            risk_new = _loss_ccy(m, p.symbol, side, lots, entry, new_sl)
            if risk_new > WIDEN_MAX_RISK_MULT * risk0 + 1e-9:
                return _refused("widen_refused", symbol, order_id, (
                    f"the new stop risks {risk_new:.2f} from entry, more than {WIDEN_MAX_RISK_MULT:g}x the trade's own "
                    f"risk at entry ({risk0:.2f}); a stop that far is a different trade"))
            state = _account_state(m)
            halt = _safety_halt_block(m, state)
            if halt and not halt.startswith("max concurrent"):
                return _refused("widen_refused", symbol, order_id, f"no added risk while halted: {halt}")
            positions = _open_positions(m)
            for q in positions:
                if q["ticket"] == int(order_id):
                    q["sl"] = float(new_sl)
            open_risk = _open_risk_ccy(m, positions)
            pending_risk = _pending_risk_ccy(m, _pending_orders(m))
            anchor = state["initial_balance"]
            worst_day = state["day_start_equity"] - state["equity"] + open_risk + pending_risk
            worst_total = anchor - state["equity"] + open_risk + pending_risk
            if worst_day >= anchor * R.CHALLENGE["max_daily_loss"] or worst_total >= anchor * R.CHALLENGE["max_total_loss"]:
                return _refused("widen_refused", symbol, order_id, (
                    f"with this stop, every open stop hitting would cost the day {worst_day:.2f} / the account "
                    f"{worst_total:.2f} {state.get('currency', '')}: at or past the FTMO limit"))
            request = {"action": m.TRADE_ACTION_SLTP, "symbol": p.symbol, "position": int(order_id),
                       "sl": float(new_sl), "tp": float(getattr(p, "tp", 0) or 0)}
            result, refused = _send(m, request, "widen_refused", symbol, order_id)
            if refused:
                return refused
            log("widen_placed", symbol=symbol, id=order_id, old_sl=cur_sl, new_sl=float(new_sl),
                risk_at_entry=round(risk0, 2), risk_new=round(risk_new, 2), why=why,
                retcode=int(result.retcode), utc=_now_utc().strftime("%Y-%m-%d %H:%M:%S"))
            return {"ok": True, "retcode": int(result.retcode), "old_sl": cur_sl, "new_sl": float(new_sl),
                    "risk_at_entry": round(risk0, 2), "risk_new": round(risk_new, 2)}
    except RuntimeError as exc:
        return _refused("widen_refused", symbol, order_id, str(exc))


def journal(text: str) -> dict:
    """One note for the live record (state/live_ledger.jsonl), the live twin of paper.py journal.
    No broker call, not gated by arming: writing down why nothing was done is always allowed."""
    text = text.strip()
    if len(text) < 10:
        return {"ok": False, "reason": "a journal note needs at least 10 characters"}
    rec = {"utc": _now_utc().strftime("%Y-%m-%d %H:%M:%S"), "day": _today_str(), "text": text}
    log("journal", **rec)
    return {"ok": True, **rec}


def ledger_rows(n: int = 0, home: Path | None = None) -> list[dict]:
    path = Path(home or HOME) / "state" / "live_ledger.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-n:] if n > 0 else rows


def closed_trades(n: int = 0, symbol: str | None = None, home: Path | None = None) -> list[dict]:
    """This worker's own closed trades, as tools/live_loop.py reconcile() recorded them from the
    broker's deal history (a stop or target hit AT the broker included)."""
    rows = [r for r in ledger_rows(home=home) if r.get("type") == "trade_closed"
            and (symbol is None or r.get("symbol") == symbol)]
    return rows[-n:] if n > 0 else rows


def trade_stats(trades: list[dict]) -> dict:
    rs = [t["r_net"] for t in trades if t.get("r_net") is not None]
    return {"closed": len(trades), "wins": sum(1 for t in trades if t.get("result") == "win"),
            "losses": sum(1 for t in trades if t.get("result") == "loss"),
            "net": round(sum(float(t.get("net") or 0) for t in trades), 2),
            "mean_r_net": round(sum(rs) / len(rs), 3) if rs else None}


def snapshot(m=None) -> dict:
    """The one live market/account picture tools/live_loop.py builds each tick and
    tools/market_feed.py publishes -- the live twin of paper.py snapshot(). Every number is read
    fresh from the broker; nothing here is cached between ticks. Also refreshes this worker's own
    peak (via _safety_halt_block), so the giveback halt tracks intraday highs, not just the
    moments an order happened to be placed."""
    m = m or _mt5_client()
    st = _account_state(m)
    cur = st["currency"]
    positions = _open_positions(m)
    own_raw = _own_positions_raw(m)
    own = []
    for p in own_raw:
        opened = R.broker_to_utc(int(p.time)).strftime("%Y-%m-%d %H:%M") if getattr(p, "time", None) else None
        own.append({"ticket": int(p.ticket), "symbol": p.symbol, "side": _position_side(p),
                    "lots": float(p.volume), "entry": float(p.price_open),
                    "sl": float(getattr(p, "sl", 0) or 0) or None, "tp": float(getattr(p, "tp", 0) or 0) or None,
                    "floating": round(float(getattr(p, "profit", 0) or 0), 2), "opened_utc": opened})
    others: dict = {}
    for p in positions:
        if p["magic"] != MAGIC:
            key = f"{p['symbol']} {p['side']}"
            others[key] = others.get(key, 0) + 1
    anchor = st["initial_balance"]
    eq = st["equity"]
    daily_limit = anchor * R.CHALLENGE["max_daily_loss"]
    total_limit = anchor * R.CHALLENGE["max_total_loss"]
    halt = _safety_halt_block(m, st)
    last_close, newest_tick = {}, 0
    for sym in R.INSTRUMENTS:
        try:
            t = m.symbol_info_tick(sym)
            if t is None:  # not in Market Watch yet: select once, ask again
                m.symbol_select(sym, True)
                t = m.symbol_info_tick(sym)
        except Exception:
            t = None
        if t is None:
            continue
        last_close[sym] = float(t.bid)
        if R.INSTRUMENTS[sym].get("class") != "crypto":  # crypto quotes all weekend: not a sign FX is open
            newest_tick = max(newest_tick, int(getattr(t, "time", 0) or 0))
    now = _now_utc()
    tick_age = (now - R.broker_to_utc(newest_tick)).total_seconds() if newest_tick else None
    trades = closed_trades()
    today = _today_str()
    placed = [r for r in ledger_rows() if r.get("type") == "order_placed"]
    cap_pct = max(R.RISK_FACTOR_MIN, min(R.MAX_RISK_PER_TRADE, st["risk_pct"]))
    return {
        "utc": now.strftime("%Y-%m-%d %H:%M UTC"), "prague_day": today, "weekday": now.strftime("%A"),
        "mode": mode(), "armed": armed(),
        "market_open": tick_age is not None and tick_age < 15 * 60,  # FX/metals/indices only, not crypto
        "newest_tick_age_s": round(tick_age) if tick_age is not None else None,
        "login": st["login"], "currency": cur, "balance": round(st["balance"], 2), "equity": round(eq, 2),
        "initial_balance": round(anchor, 2), "day_start_equity": round(st["day_start_equity"], 2),
        "day_pnl": round(eq - st["day_start_equity"], 2),
        "daily_loss_used": round(st["day_start_equity"] - eq, 2), "daily_loss_limit": round(daily_limit, 2),
        "total_loss_used": round(anchor - eq, 2), "total_loss_limit": round(total_limit, 2),
        "risk_pct": cap_pct, "per_trade_cap": round(cap_pct * st["balance"], 2),
        "own_positions": own, "own_open_risk": round(_open_risk_ccy(m, [p for p in positions if p["magic"] == MAGIC]), 2),
        "own_day_pnl": round(_own_day_pnl_usd(m), 2), "own_halt": halt,
        "entry_block": _entry_window_block(now),
        "other_runners_positions": others,
        "own_traded_days": sorted({r.get("day") for r in placed if r.get("day")}),
        "own_trades_today": sum(1 for r in placed if r.get("day") == today),
        "own_closed_trades": trade_stats(trades),
        "last_close": last_close,
    }


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="live_broker.py", description=__doc__)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status")
    j = sub.add_parser("journal")
    j.add_argument("--text", required=True)
    rf = sub.add_parser("risk-factor")
    rf.add_argument("--set", dest="set_pct", type=float)
    rf.add_argument("--why", default="")
    rf.add_argument("--day-halt", dest="day_halt", type=float, help="own day-loss halt, fraction (0.015 = 1.5%%)")
    rf.add_argument("--giveback", type=float, help="own giveback-from-peak halt, fraction (0.03 = 3%%)")
    rf.add_argument("--max-positions", dest="max_positions", type=int, help="own concurrent-position cap")
    o = sub.add_parser("order")
    o.add_argument("--symbol", required=True)
    o.add_argument("--side", choices=["buy", "sell"], required=True)
    o.add_argument("--lots", type=float)
    o.add_argument("--risk-pct", type=float, dest="risk_pct",
                   help="size from risk instead of --lots: PERCENT of balance (0.3 = 0.3%%)")
    o.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="place nothing: show the broker's risk per lot, the cap, and the lots that fit it")
    o.add_argument("--sl", type=float, required=True)
    o.add_argument("--tp", type=float)
    o.add_argument("--why", default="")
    c = sub.add_parser("close")
    c.add_argument("--symbol", required=True)
    c.add_argument("id", type=int)
    c.add_argument("--volume", type=float, help="close only this many lots (a partial close)")
    c.add_argument("--why", required=True)
    m = sub.add_parser("modify")
    m.add_argument("--symbol", required=True)
    m.add_argument("id", type=int)
    m.add_argument("--sl", type=float)
    m.add_argument("--tp", type=float)
    m.add_argument("--why", required=True)
    w = sub.add_parser("widen")
    w.add_argument("--symbol", required=True)
    w.add_argument("id", type=int)
    w.add_argument("--sl", type=float, required=True)
    w.add_argument("--why", required=True)
    a = p.parse_args(argv)

    if a.cmd == "status" or a.cmd is None:
        print(json.dumps(status(), indent=1, default=str))
    elif a.cmd == "order" and a.dry_run:
        try:
            res = size(a.symbol, a.side, a.sl, lots=a.lots, risk_pct=a.risk_pct)
        except Exception as exc:  # a dry run that cannot read the broker is an answer, not a traceback
            res = {"ok": False, "reason": f"broker read failed: {exc}"}
        print(json.dumps(res, indent=1, default=str))
    elif a.cmd == "order":
        if (a.lots is None) == (a.risk_pct is None):
            res = {"ok": False, "reason": "give exactly one of --lots or --risk-pct (PERCENT of balance)"}
        else:
            res = place_order(a.symbol, a.side, a.lots, a.sl, a.tp, a.why, risk_pct=a.risk_pct)
        print(json.dumps(res, indent=1, default=str))
    elif a.cmd == "close":
        print(json.dumps(close_position(a.symbol, a.id, a.why, a.volume), indent=1, default=str))
    elif a.cmd == "modify":
        print(json.dumps(modify_position(a.symbol, a.id, a.sl, a.tp, a.why), indent=1, default=str))
    elif a.cmd == "widen":
        print(json.dumps(widen_stop(a.symbol, a.id, a.sl, a.why), indent=1, default=str))
    elif a.cmd == "journal":
        print(json.dumps(journal(a.text), indent=1, default=str))
    elif a.cmd == "risk-factor":
        levers = any(v is not None for v in (a.day_halt, a.giveback, a.max_positions))
        if a.set_pct is None and not levers:
            res = risk_factor()
        else:
            res = {}
            if a.set_pct is not None:
                res["risk_pct"] = set_risk_factor(a.set_pct, a.why)
            if levers:
                res["levers"] = set_risk_levers(a.why, a.day_halt, a.giveback, a.max_positions)
        print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
