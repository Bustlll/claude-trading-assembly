"""LIVE MARKET: read-only views of the real market and this worker's own live record.

The live twin of paper.py's read commands, against the shared MT5 bridge (tools/live_broker.py
_mt5_client). It has NO order verb: a kind that is given this tool and not live_broker.py's
order/close/modify can look at anything and move nothing.

  live_market.py symbols                                every tradable symbol: bid/ask, spread now, lot rules, and the
                                                        risk table (contract size, value of a 1.0 move per lot)
  live_market.py symbols --find TEXT                    search EVERYTHING the broker offers (stocks, commodities...)
  live_market.py symbols --enable SYMBOL                activate a symbol in the terminal and show its quote
  live_market.py bars SYMBOL [-n 24] [--tf M5|M15|H1|H4|D1] [--session day|night]
                                                        any broker symbol, not only the tradable ones; an inactive
                                                        one is activated for you (orders stay limited to `symbols`)
  live_market.py trades [-n 10] [--symbol S]            this worker's closed live trades + what they share
  live_market.py ledger [-n 30]                         the last rows of state/live_ledger.jsonl
  live_market.py alert --symbol S --above P|--below P --sl P --tp P --why "plan" [--when touch|close|hold|reject] [--tf M5|M15|H1]
                 [--bars N] [--hours H] [--from WORKER]   a price alert (tools/live_alerts.py; places nothing)
  live_market.py alert --cancel ID --why "..." | alerts

Bars follow paper.py's conventions exactly, so what training learned to read reads the same:
  * times are TRUE UTC (the broker clock is NY+7h; guard/rules.py broker_to_utc), day = Prague day
  * only CLOSED bars: the still-forming bar is never shown (no repaint)
  * M5..H4 are the broker's own bars (same bucket edges as paper.py's resample, both on the
    broker clock); D1 is built from M5 by PRAGUE day, as paper.py does -- not the broker's D1,
    whose day turns at broker midnight, an hour away from FTMO's reset
  * session: night if the Prague hour is >= 22 or < 7 (tools/build_specs.py), else day
Bars come from copy_rates_from_pos (the live series). copy_rates_range is the history cache and
trails the live edge by ~2h (deltaForceFTMO, 2026-09-14); it is not used here.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from guard import rules as R  # noqa: E402
import live_broker as LB  # noqa: E402

TF_MT5 = {"M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15", "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4"}
TF_SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400}
PRAGUE = ZoneInfo(R.DAY_TZ)
MAX_BARS = 500


def _session(t_broker: int) -> str:
    hour = R.broker_to_utc(t_broker).astimezone(PRAGUE).hour
    return "night" if (hour >= 22 or hour < 7) else "day"


def _day_key(t_broker: int) -> str:
    return R.broker_to_utc(t_broker).astimezone(PRAGUE).date().isoformat()


def _now_broker() -> int:
    return LB.utc_to_broker_epoch(LB._now_utc())


def _closed(rates, tf_seconds: int, now_b: int) -> list[dict]:
    """Rates as dicts, minus a last bar that has not closed yet (open + its length > now)."""
    out = [{"t": int(r["time"]), "o": float(r["open"]), "h": float(r["high"]), "l": float(r["low"]),
            "c": float(r["close"])} for r in (rates if rates is not None else [])]
    if out and out[-1]["t"] + tf_seconds > now_b:
        out.pop()
    return out


def _row(b: dict, tf: str, session: str) -> dict:
    return {"utc": R.broker_to_utc(b["t"]).strftime("%Y-%m-%d %H:%M"), "day": _day_key(b["t"]), "tf": tf,
            "session": session, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}


def _rates(m, fetch, symbol: str):
    """One fetch; if the broker returns nothing, activate the symbol (2026-10-12: a symbol not in the
    terminal's Market Watch can come back empty) and retry while its history loads. Returns (rates,
    activation) -- activation is None when the first fetch already had data."""
    rates = fetch()
    if rates is not None and len(rates):
        return rates, None
    act = LB.ensure_symbol(m, symbol)
    if act["status"] in ("not_at_broker", "select_failed", "read_failed"):
        return None, act
    for _ in range(4):
        rates = fetch()
        if rates is not None and len(rates):
            return rates, act
        time.sleep(1.0)
    return None, act


def _no_bars(symbol: str, tf: str, act: dict | None) -> list[dict]:
    why = (act or {}).get("reason") or (f"{symbol} is active but the broker sent no {tf} history yet "
                                        "(it may still be downloading) -- try again in a minute")
    return [{"error": f"no {tf} bars for {symbol}: {why}"}]


def bars(symbol: str, n: int = 24, tf: str = "M5", session: str | None = None, m=None) -> list[dict]:
    """Any symbol the broker offers can be READ here (2026-10-12; before, only guard/rules.py
    INSTRUMENTS). Ordering is still limited to INSTRUMENTS -- live_broker.py's order gate refuses the rest."""
    if tf not in (*TF_MT5, "D1"):
        return [{"error": f"--tf must be one of M5, M15, H1, H4, D1, not {tf}"}]
    n = max(1, min(int(n), MAX_BARS))
    m = m or LB._mt5_client()
    try:
        m.symbol_select(symbol, True)
    except Exception:
        pass
    now_b = _now_broker()
    if tf == "D1":
        import paper as P  # paper.py's own resample: D1 by Prague day, forming day dropped
        rates, act = _rates(m, lambda: m.copy_rates_from_pos(symbol, m.TIMEFRAME_M5, 0, min(50_000, (n + 2) * 288)), symbol)
        if rates is None:
            return _no_bars(symbol, "D1", act)
        m5 = [{**b, "session": _session(b["t"])} for b in _closed(rates, 300, now_b)]
        rows = P.resample(m5, "D1", now_b)
        return [_row(b, "D1", b["session"]) for b in rows[-n:]]
    want = n * 3 if (session in ("day", "night") and tf == "M5") else n
    rates, act = _rates(m, lambda: m.copy_rates_from_pos(symbol, getattr(m, TF_MT5[tf]), 0, want + 1), symbol)
    if rates is None:
        return _no_bars(symbol, tf, act)
    out = []
    for b in _closed(rates, TF_SECONDS[tf], now_b):
        sess = _session(b["t"] + TF_SECONDS[tf] - 300)  # the session of the bucket's LAST M5 bar, as paper.py
        if session in ("day", "night") and tf == "M5" and sess != session:
            continue
        out.append(_row(b, tf, sess))
    return out[-n:]


def watch(symbol: str, ticket: int, above=None, below=None, in_min=None, quiet=False, quiet_min=None, clear=False,
          why: str = "", m=None, home=None, now=None) -> dict:
    """The position manager's standing watch (2026-10-20; tools/manage_core.py set_watch, the paper twin is paper.py
    watch): re-wake at a level or a time, or QUIET -- let it rip. Places nothing at the broker. Only this assembly's
    own open positions (MAGIC)."""
    import live_loop as LL
    import manage_core as MC
    m = m or LB._mt5_client()
    p = next((x for x in LB._own_positions_raw(m) if int(x.ticket) == int(ticket)), None)
    if p is None:
        return {"ok": False, "reason": f"{ticket} is not one of this assembly's open positions"}
    if p.symbol != symbol:
        return {"ok": False, "reason": f"position {ticket} is {p.symbol}, not {symbol}"}
    tick = m.symbol_info_tick(symbol)
    price = float(tick.bid) if tick is not None else None      # the bars (and so the watch) are bid
    home = home or LB.HOME
    path = LL._manage_path(home)
    st_all = LL._read(path, {})
    st, rep = MC.set_watch(st_all.get(str(int(ticket))), now or LB._now_utc(), side=LB._position_side(p), price=price,
                           above=above, below=below, in_min=in_min, quiet=quiet, quiet_min=quiet_min, clear=clear,
                           why=why)
    if rep["ok"]:
        st_all[str(int(ticket))] = st
        LL._write(path, st_all)
        LB.log("manage_watch", symbol=symbol, id=int(ticket), watch=rep.get("watch"), why=(why or "").strip())
    return rep


def m5_history(symbol: str, n: int, m=None) -> list[dict]:
    """The last n CLOSED M5 bars, past bars()' MAX_BARS -- for code that needs days of history (the position
    manager's burst baseline, the timing specialist's hour table; 2026-10-19). Same row shape as bars()."""
    m = m or LB._mt5_client()
    rates, act = _rates(m, lambda: m.copy_rates_from_pos(symbol, m.TIMEFRAME_M5, 0, int(n) + 1), symbol)
    if rates is None:
        return []
    return [_row(b, "M5", _session(b["t"])) for b in _closed(rates, 300, _now_broker())][-int(n):]


def find_symbols(text: str, m=None, limit: int = 60) -> list[dict]:
    """Search everything the broker offers (one filtered catalogue read, done broker-side), e.g. NVDA,
    COCOA, GER. `active` = already in the terminal's Market Watch; bars activates it either way."""
    m = m or LB._mt5_client()
    text = (text or "").strip()
    if not text:
        return [{"error": "give a search text, e.g. symbols --find NVDA"}]
    found = m.symbols_get(group=f"*{text}*") or []
    if not found:  # some builds match the group mask case-sensitively
        low = text.lower()
        found = [s for s in (m.symbols_get() or []) if low in s.name.lower()
                 or low in str(getattr(s, "description", "")).lower()]
    out = [{"symbol": s.name, "description": getattr(s, "description", ""), "path": getattr(s, "path", ""),
            "active": bool(getattr(s, "visible", False)), "tradable_here": s.name in R.INSTRUMENTS}
           for s in found[:limit]]
    return out or [{"error": f"nothing at this broker matches {text!r}"}]


def symbols(m=None) -> list[dict]:
    m = m or LB._mt5_client()
    now = LB._now_utc()
    out = []
    for sym in sorted(R.INSTRUMENTS):
        sp = R.spec(sym)
        row = {"symbol": sym, "class": sp["class"], "commission_rt_per_lot_usd": sp["commission_rt_per_lot"]}
        try:
            tick = m.symbol_info_tick(sym)
            if tick is None:  # not in Market Watch yet: select it once, then ask again
                m.symbol_select(sym, True)
                tick = m.symbol_info_tick(sym)
            si = m.symbol_info(sym)
        except Exception as exc:
            out.append({**row, "error": str(exc)})
            continue
        if tick is None or si is None:
            out.append({**row, "error": "no quote from the broker"})
            continue
        bid, ask = float(tick.bid), float(tick.ask)
        age = (now - R.broker_to_utc(int(getattr(tick, "time", 0) or 0))).total_seconds() if getattr(tick, "time", 0) else None
        tsz = float(getattr(si, "trade_tick_size", 0) or sp["tick_size"])
        tval = float(getattr(si, "trade_tick_value", 0) or 0) or None
        out.append({**row, "bid": bid, "ask": ask, "spread_now": round(ask - bid, 6),
                    "quote_age_s": round(age) if age is not None else None,
                    "volume_min": float(getattr(si, "volume_min", 0) or sp["volume_min"]),
                    "volume_step": float(getattr(si, "volume_step", 0) or sp["volume_step"]),
                    "tick_size": tsz, "tick_value_account_ccy_per_lot": tval,
                    # the risk table (2026-10-12): what ONE lot is, and what a 1.0 price move costs per lot,
                    # from the broker -- and the same figure in the table deltaForceFTMO's runners gate on
                    "contract_size": float(getattr(si, "trade_contract_size", 0) or sp.get("contract_size") or 0) or None,
                    "account_ccy_per_1.0_price_move_per_lot": round(tval / tsz, 4) if (tval and tsz) else None,
                    "runner_table_usd_per_1.0_price_move_per_lot": LB.runner_table_uppl(sym)})
    return out


def bars_text(symbol: str, tf: str, rows: list[dict]) -> str:
    """paper.py's own table (2026-10-18): the live desk reads bars in the form training learned on, the same
    fields at full precision (the per-bar JSON repeated every key name on every bar)."""
    import paper as P
    return P.bars_text(symbol, tf, rows)


def trades(n: int = 10, symbol: str | None = None, home=None) -> dict:
    rows = LB.closed_trades(n=0, symbol=symbol, home=home)
    return {"summary": LB.trade_stats(rows), "last": rows[-n:] if n > 0 else rows}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(prog="live_market.py", description=__doc__)
    sub = p.add_subparsers(dest="cmd")
    sy = sub.add_parser("symbols")
    sy.add_argument("--find", metavar="TEXT")
    sy.add_argument("--enable", metavar="SYMBOL")
    b = sub.add_parser("bars")
    b.add_argument("symbol")
    b.add_argument("-n", type=int, default=24)
    b.add_argument("--tf", default="M5", choices=["M5", "M15", "H1", "H4", "D1"])
    b.add_argument("--session", choices=["day", "night"])
    t = sub.add_parser("trades")
    t.add_argument("-n", type=int, default=10)
    t.add_argument("--symbol")
    lg = sub.add_parser("ledger")
    lg.add_argument("-n", type=int, default=30)
    al = sub.add_parser("alert")
    al.add_argument("--symbol")
    al.add_argument("--above", type=float)
    al.add_argument("--below", type=float)
    al.add_argument("--why", default="")
    al.add_argument("--when", choices=["touch", "close", "hold", "reject"], default="touch")
    al.add_argument("--tf", choices=["M5", "M15", "H1"], default="M15")
    al.add_argument("--bars", type=int, default=2)
    al.add_argument("--hours", type=float, default=8.0)
    al.add_argument("--from", dest="worker", default="trade")
    al.add_argument("--cancel", type=int)
    al.add_argument("--sl", type=float)
    al.add_argument("--tp", type=float)
    sub.add_parser("alerts")
    wt = sub.add_parser("watch")
    wt.add_argument("--symbol", required=True)
    wt.add_argument("id", type=int)
    wt.add_argument("--above", type=float)
    wt.add_argument("--below", type=float)
    wt.add_argument("--in", dest="in_min", type=int)
    wt.add_argument("--quiet", action="store_true")
    wt.add_argument("--for", dest="quiet_min", type=int)
    wt.add_argument("--clear", action="store_true")
    wt.add_argument("--why", default="")
    a = p.parse_args(argv)
    try:
        if a.cmd == "symbols" and a.enable:
            res = LB.ensure_symbol(LB._mt5_client(), a.enable)
        elif a.cmd == "symbols" and a.find:
            res = find_symbols(a.find)
        elif a.cmd == "symbols":
            res = symbols()
        elif a.cmd == "bars":
            print(bars_text(a.symbol, a.tf, bars(a.symbol, a.n, a.tf, a.session)))
            return 0
        elif a.cmd == "trades":
            res = trades(a.n, a.symbol)
        elif a.cmd == "ledger":
            res = LB.ledger_rows(a.n)
        elif a.cmd == "alert":
            import live_alerts as LA
            if a.cancel is not None:
                res = LA.cancel(a.cancel, a.why or "cancelled")
            elif not a.symbol:
                res = {"ok": False, "reason": "--symbol is required (or --cancel ID)"}
            else:
                res = LA.set_alert(a.symbol, a.above, a.below, a.why, a.hours, a.worker, a.when, a.tf, a.bars,
                                   sl=a.sl, tp=a.tp, require_plan=True)
            print(json.dumps(res, separators=(",", ":"), default=str))
            return 0
        elif a.cmd == "watch":
            print(json.dumps(watch(a.symbol, a.id, a.above, a.below, a.in_min, a.quiet, a.quiet_min, a.clear, a.why),
                             indent=1, default=str))
            return 0
        elif a.cmd == "alerts":
            import live_alerts as LA
            print(json.dumps(LA.active(), separators=(",", ":"), default=str))
            return 0
        else:
            print(__doc__)
            return 0
    except Exception as exc:  # a broker that cannot be reached is an answer, not a traceback
        res = {"error": f"broker read failed: {exc}"}
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
