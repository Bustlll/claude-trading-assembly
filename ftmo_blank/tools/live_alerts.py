"""LIVE PRICE ALERTS (2026-10-18): training's alerts on the real account's closed bars.

The rules are tools/alerts_core.py, the same functions tools/paper.py runs on the replay, so the same alert
fires on the same bar on both desks. Live adds only the clock and the storage:
  * state/live_alerts.json   the waiting alerts (at most alerts_core.MAX_ACTIVE)
  * state/live_ledger.jsonl  alert_set / alert_cancelled / alert_expired / level_alert rows (live_broker.log)
  * check() runs on every live tick (tools/live_loop.py): every bar of the alert's timeframe that has CLOSED
    since the last check is evaluated exactly once, in order; a condition met inside the trading window fires
    a live_level_alert event, which wakes that symbol's live trader (or the decider) once, with the plan.
Setting an alert places nothing at the broker: it is a note to the desk's own code.

  live_market.py alert --symbol S --above P|--below P --sl P --tp P --why "plan" [--when touch|close|hold|reject]
                       [--tf M5|M15|H1] [--bars N] [--hours H] [--from WORKER]   (--sl/--tp: the plan's mock levels)
  live_market.py alert --cancel ID --why "..."
  live_market.py alerts
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from guard import rules as R  # noqa: E402
import alerts_core as AC  # noqa: E402
import live_broker as LB  # noqa: E402

TF_CODE = {"M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15", "H1": "TIMEFRAME_H1"}


def _path(home) -> Path:
    return Path(home) / "state" / "live_alerts.json"


def _load(home) -> dict:
    try:
        return json.loads(_path(home).read_text())
    except (OSError, ValueError):
        return {"alerts": [], "next_id": 1}


def _save(st: dict, home) -> None:
    p = _path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(p)


def _utc(t_broker: int) -> str:
    return R.broker_to_utc(t_broker).strftime("%Y-%m-%d %H:%M UTC")


def _closed(m, symbol: str, tf: str, n: int, now: dt.datetime) -> list[dict]:
    import live_market as LM
    rates = m.copy_rates_from_pos(symbol, getattr(m, TF_CODE[tf]), 0, n + 1)
    return LM._closed(rates, AC.TF_SECONDS[tf], LB.utc_to_broker_epoch(now))


def active(home=HOME) -> list[dict]:
    return _load(home)["alerts"]


def set_alert(symbol: str, above, below, why: str, hours: float = AC.DEFAULT_HOURS, worker: str = "trade",
              when: str = "touch", tf: str = "M15", nbars: int = 2, m=None, now=None, home=HOME,
              sl=None, tp=None, require_plan: bool = False) -> dict:
    if symbol not in R.INSTRUMENTS:
        return {"ok": False, "reason": f"{symbol} is not tradable on this account; alerts are for symbols you can "
                                       "order (live_market.py symbols lists them)"}
    now = now or LB._now_utc()
    m = m or LB._mt5_client()
    m5 = _closed(m, symbol, "M5", 3, now)
    if not m5:
        return {"ok": False, "reason": f"no closed M5 bar for {symbol} yet (market shut or not active)"}
    st = _load(home)
    err, spec = AC.check_new(symbol, above, below, why, m5[-1]["c"], AC.atr(_closed(m, symbol, "M15", 15, now)),
                             when, tf, nbars, hours, st["alerts"], sl, tp, require_plan)
    if err:
        return {"ok": False, "reason": err}
    last = _closed(m, symbol, spec["tf"], 2, now)
    expires = now + dt.timedelta(hours=spec.pop("hours"))
    aid = st["next_id"]
    rec = {"id": aid, **spec, "by": worker, "set_utc": now.strftime("%Y-%m-%d %H:%M UTC"),
           "checked_t": last[-1]["t"] if last else 0,       # only bars closing after this one are judged
           "expires_t": LB.utc_to_broker_epoch(expires), "expires_utc": expires.strftime("%Y-%m-%d %H:%M UTC")}
    st["alerts"].append(rec)
    st["next_id"] = aid + 1
    _save(st, home)
    LB.log("alert_set", **{k: rec[k] for k in ("id", "symbol", "dir", "level", "why", "by", "when", "tf", "nbars",
                                               "ref", "expires_utc", "plan_side", "plan_sl", "plan_tp")})
    return {"ok": True, "id": aid, "fires": f"{AC.text(rec)} inside the window, before {rec['expires_utc']}",
            "wakes": "that symbol's live trader if it has one, else the decider, with your --why as the plan"}


def cancel(aid: int, why: str, home=HOME) -> dict:
    st = _load(home)
    a = next((x for x in st["alerts"] if x["id"] == aid), None)
    if a is None:
        return {"ok": False, "reason": f"no waiting alert {aid}; waiting: {[x['id'] for x in st['alerts']]}"}
    st["alerts"].remove(a)
    _save(st, home)
    LB.log("alert_cancelled", id=aid, symbol=a["symbol"], why=why.strip())
    return {"ok": True, "cancelled": aid}


def check(m, now: dt.datetime, in_window, home=HOME) -> list[dict]:
    """Judge every bar that closed since the last check, for every waiting alert. Returns live_level_alert
    events. in_window(utc datetime) -> bool is the live trading window (tools/live_loop.py)."""
    st = _load(home)
    if not st["alerts"]:
        return []
    events, changed = [], False
    for a in list(st["alerts"]):
        tf = a.get("tf", "M5") if a.get("when") != "touch" else "M5"
        try:
            bars = [b for b in _closed(m, a["symbol"], tf, 30, now) if b["t"] > a.get("checked_t", 0)]
        except Exception:
            continue                                   # an unreadable symbol this tick: judged next tick
        for b in bars:
            a["checked_t"] = b["t"]
            changed = True
            last_m5 = b["t"] + AC.TF_SECONDS[tf] - 300   # the M5 bar that completes it: paper.py's clock for the bar
            if last_m5 > a["expires_t"]:
                break
            hit = AC.step(a, b)
            if hit and not in_window(R.broker_to_utc(last_m5 + 300)):
                AC.outside_window(a)
                continue
            if hit:
                st["alerts"].remove(a)
                rec = {k: a.get(k) for k in AC.HIT_KEYS}
                row = {**rec, "condition": AC.text(a), "bar": {k: b[k] for k in ("o", "h", "l", "c")},
                       "touched_utc": _utc(last_m5)}
                LB.log("level_alert", **row)
                events.append({"type": "live_level_alert", **row})
                break
        else:
            if a in st["alerts"] and LB.utc_to_broker_epoch(now) > a["expires_t"]:
                st["alerts"].remove(a)
                LB.log("alert_expired", id=a["id"], symbol=a["symbol"], level=a["level"])
                changed = True
            continue
        if a in st["alerts"] and a.get("checked_t", 0) + AC.TF_SECONDS[tf] - 300 > a["expires_t"]:
            st["alerts"].remove(a)
            LB.log("alert_expired", id=a["id"], symbol=a["symbol"], level=a["level"])
    if changed or events:
        _save(st, home)
    return events
