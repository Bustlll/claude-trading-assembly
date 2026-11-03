"""Shared test doubles for the LIVE modules (live_broker, live_market, live_loop, driver live-tick).

FakeMT5 is the surface those modules call on pymt5linux's MetaTrader5, with the real MT5 integer
constants (confirmed read-only against the bridge: FOK=0 IOC=1 RETURN=2, BUY=0 SELL=1, DEAL=1
SLTP=6, GTC=0, RETCODE_DONE=10009, TIMEFRAME_M5=5, TIMEFRAME_H1=16385). Every test patches
live_broker._mt5_client to return one of these, so no test ever reaches the real shared bridge.

Times follow the real broker: positions/deals/rates carry BROKER-clock epochs (NY+7h), and
history_deals_get returns deals by POSITION when asked, or everything (the caller filters by true
UTC itself -- live_broker._deals_between -- exactly because the real bridge's own date bounds are
on the broker clock).
"""
import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "tools"))
sys.path.insert(0, str(HOME))
import live_broker as L  # noqa: E402
from guard import rules as R  # noqa: E402

# A Wednesday, mid-session: no entry gate, a decision hour can be chosen around it.
PINNED_NOW = dt.datetime(2026, 10, 7, 10, 0, tzinfo=dt.timezone.utc)


def broker_epoch(t: dt.datetime) -> int:
    return L.utc_to_broker_epoch(t)


def no_broker():
    raise RuntimeError("simulated: no broker reachable")


class FakeMT5:
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 6
    ORDER_TIME_GTC = 0
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_PLACED = 10008
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_H1 = 16385
    TIMEFRAME_H4 = 16388

    def __init__(self, balance=10_000.0, equity=10_000.0, login=1234567, currency="USD", fx=1.0,
                 positions=None, orders=None, deals=None, tick_bid=1.10000, tick_ask=1.10010, tick_time=None,
                 send_retcode=10009, send_raises=None, send_returns_none=False, rates=None, ticks=None):
        self._balance = balance; self._equity = equity; self._login = login; self._currency = currency
        self.fx = fx  # account currency per USD: 1/1.09 for a EUR account when EURUSD = 1.09
        self._positions = positions or []; self._orders = orders or []; self._deals = deals or []
        self._tick_bid = tick_bid; self._tick_ask = tick_ask
        self._tick_time = tick_time if tick_time is not None else broker_epoch(PINNED_NOW)
        self._ticks = ticks or {}  # per-symbol (bid, ask) overrides
        self._send_retcode = send_retcode; self._send_raises = send_raises
        self._send_returns_none = send_returns_none
        self._rates = rates or {}  # (symbol, timeframe) -> list of rate dicts
        self.sent = []

    def initialize(self):
        return True

    def last_error(self):
        return (0, "ok")

    def account_info(self):
        return SimpleNamespace(balance=self._balance, equity=self._equity, login=self._login,
                               currency=self._currency)

    def positions_get(self, ticket=None):
        if ticket is not None:
            return tuple(p for p in self._positions if p.ticket == ticket)
        return tuple(self._positions)

    def orders_get(self):
        return tuple(self._orders)

    def history_deals_get(self, date_from=None, date_to=None, position=None):
        if position is not None:
            return tuple(d for d in self._deals if getattr(d, "position_id", None) == position)
        return tuple(self._deals)

    def symbol_select(self, symbol, enable):
        return True

    def symbol_info(self, symbol):
        return SimpleNamespace(filling_mode=3, point=0.0001, volume_min=0.01, volume_step=0.01,
                               trade_tick_size=0.00001, trade_tick_value=1.0)

    def symbol_info_tick(self, symbol):
        bid, ask = self._ticks.get(symbol, (self._tick_bid, self._tick_ask))
        return SimpleNamespace(bid=bid, ask=ask, time=self._tick_time)

    def order_calc_profit(self, otype, symbol, volume, price_open, price_close):
        move = (price_close - price_open) if otype == self.ORDER_TYPE_BUY else (price_open - price_close)
        return move * volume * R.spec(symbol)["usd_per_price_unit_per_lot"] * self.fx

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        rows = self._rates.get((symbol, timeframe), [])
        end = len(rows) - start_pos
        return rows[max(0, end - count):end] if end > 0 else []

    def order_send(self, request):
        self.sent.append(request)
        if self._send_raises is not None:
            raise self._send_raises
        if self._send_returns_none:
            return None
        return SimpleNamespace(retcode=self._send_retcode, order=555, deal=0,
                               volume=request.get("volume"), price=request.get("price"), comment="")


def pos(ticket, symbol, side, lots, entry, sl=None, tp=None, magic=0, comment="", profit=0.0, opened=None):
    return SimpleNamespace(ticket=ticket, symbol=symbol, type=(0 if side == "buy" else 1),
                           volume=lots, price_open=entry, sl=sl or 0.0, tp=tp or 0.0,
                           magic=magic, comment=comment, profit=profit,
                           time=broker_epoch(opened or PINNED_NOW - dt.timedelta(hours=1)))


def deal(symbol, profit, magic=0, commission=0.0, swap=0.0, when=None, position_id=0, entry=1, reason=4,
         price=1.0, volume=0.01, dtype=1, comment=""):
    """when is a true-UTC datetime (default: PINNED_NOW); the deal carries the broker-clock epoch."""
    return SimpleNamespace(symbol=symbol, profit=profit, commission=commission, swap=swap, fee=0.0, magic=magic,
                           time=broker_epoch(when or PINNED_NOW), position_id=position_id, entry=entry,
                           reason=reason, price=price, volume=volume, type=dtype, comment=comment)


def m5_rates(start_close: float, closes_step: float, n: int, end_utc: dt.datetime) -> list[dict]:
    """n M5 bars whose LAST bar opens at end_utc - 5min (so all n are closed at end_utc)."""
    out = []
    first = end_utc - dt.timedelta(minutes=5 * n)
    for i in range(n):
        t = broker_epoch(first + dt.timedelta(minutes=5 * i))
        c = start_close + closes_step * (i + 1)
        o = start_close + closes_step * i
        out.append({"time": t, "open": o, "high": max(o, c), "low": min(o, c), "close": c})
    return out


def point_live_broker_at(tmp: Path):
    """Redirect every live_broker state path into tmp, pin the clock, return a restore callback."""
    saved = {k: getattr(L, k) for k in ("HOME", "LOCK_PATH", "LEDGER_PATH", "DAY_STATE_PATH",
                                        "ACCOUNTS_STATE_PATH", "PEAK_STATE_PATH", "LIVE_RISK_PATH", "DELTA_LEDGER_PATH", "_mt5_client",
                                        "_now_utc")}
    L.HOME = tmp
    L.LOCK_PATH = tmp / "state" / "live_broker.lock"
    L.LEDGER_PATH = tmp / "state" / "live_ledger.jsonl"
    L.DAY_STATE_PATH = tmp / "state" / "live_day.json"
    L.ACCOUNTS_STATE_PATH = tmp / "state" / "live_accounts.json"
    L.PEAK_STATE_PATH = tmp / "state" / "live_peak.json"
    L.LIVE_RISK_PATH = tmp / "state" / "live_risk.json"
    L.DELTA_LEDGER_PATH = tmp / "delta_ledgers" / "live_trades.jsonl"   # NEVER the real shared ledger
    L.DELTA_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    L._now_utc = lambda: PINNED_NOW

    def restore():
        for k, v in saved.items():
            setattr(L, k, v)
    return restore
