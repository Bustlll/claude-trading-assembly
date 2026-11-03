"""MEASURE SPREADS: each instrument's typical spread and its rollover spike, from the broker's own bars.

  python3 tools/measure_spreads.py [--days 14] [--write]

guard/instruments.json carried spread 0 for the 11 instruments build_specs.py took from the broker
registry (it has no spread field): UKOIL/USOIL, the USD exotics, XAU/XAGEUR, XCU/XPD/XPT. So the paper
replay filled them with NO spread cost, and the live watchdog widened their stops by 0 at the rollover
(2026-10-12: a USDMXN stop taken out by a spread spike the bid never reached).

Source: the `spread` column of the broker's own M5 bars (copy_rates_from_pos), in points x point.
Checked 2026-10-13 against live quotes: USOIL median 0.075 vs live 0.086, USDMXN 0.0047 vs 0.0033,
XPDUSD 6.72 vs 6.4, USDZAR 0.0097 vs 0.0104. It is NOT usable for raw-spread FX majors (EURUSD reads
0 on 85% of bars): their table value stays the manifest snapshot, and they are flagged `raw_spread`.

Per instrument, in PRICE units: median (the typical spread), p90, max, and over the two hours that
start at the daily rollover (17:00 New York, the broker's midnight) rollover_p95 and rollover_max --
the spike a stop must survive. --write saves knowledge/measured_spreads.json (no dates, no account:
the training replay reads knowledge/ and must not learn the live calendar) and appends the provenance
(when, which account, how many bars) to state/spread_measurements.jsonl.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from guard import rules as R  # noqa: E402
import live_broker as LB  # noqa: E402

OUT = HOME / "knowledge" / "measured_spreads.json"
LOG = HOME / "state" / "spread_measurements.jsonl"
_NY = ZoneInfo("America/New_York")
ROLLOVER_SPAN_H = 2


def rollover_start_utc(day_utc: dt.date) -> dt.datetime:
    """The daily rollover = 17:00 New York (the broker clock is NY+7, so this is its midnight)."""
    ny = dt.datetime(day_utc.year, day_utc.month, day_utc.day, 17, 0, tzinfo=_NY)
    return ny.astimezone(dt.timezone.utc)


def _pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    if not s:
        return 0.0
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def stats(rates, point: float) -> dict:
    """rates: the broker's M5 bars (time = broker epoch, spread = points)."""
    all_sp, roll = [], []
    for r in rates:
        sp = float(r["spread"]) * point
        all_sp.append(sp)
        t = R.broker_to_utc(int(r["time"]))
        start = rollover_start_utc(t.date())
        if start <= t < start + dt.timedelta(hours=ROLLOVER_SPAN_H):
            roll.append(sp)
    if not all_sp:
        return {"n_bars": 0}
    zeros = sum(1 for x in all_sp if x == 0) / len(all_sp)
    out = {"median": round(_pct(all_sp, 0.5), 8), "p90": round(_pct(all_sp, 0.9), 8),
           "max": round(max(all_sp), 8), "rollover_p95": round(_pct(roll, 0.95), 8) if roll else None,
           "rollover_max": round(max(roll), 8) if roll else None, "n_bars": len(all_sp),
           "n_rollover_bars": len(roll)}
    if zeros > 0.5:
        out["raw_spread"] = True   # commission account majors: the bar column reads ~0, not usable
    return out


def measure(days: int = 14, m=None, symbols=None) -> dict:
    m = m or LB._mt5_client()
    res = {}
    for sym in sorted(symbols or R.INSTRUMENTS):
        try:
            si = m.symbol_info(sym)
            point = float(getattr(si, "point", 0) or R.spec(sym)["point"])
            rates = m.copy_rates_from_pos(sym, m.TIMEFRAME_M5, 0, 288 * days)
        except Exception as exc:
            res[sym] = {"error": str(exc)}
            continue
        res[sym] = stats(rates if rates is not None else [], point) if rates is not None and len(rates) \
            else {"error": "no bars from the broker"}
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    m = LB._mt5_client()
    res = measure(a.days, m=m)
    if a.write:
        about = ("Each instrument's spread in PRICE units, measured from the broker's own five-minute bars over "
                 f"about {a.days} calendar days: median = the typical spread; p90; max; rollover_p95 / rollover_max = "
                 f"the spread in the {ROLLOVER_SPAN_H} hours after the daily rollover (17:00 New York), when it spikes. "
                 "raw_spread = a commission-account major whose bar spread reads ~0 (use the table's figure).")
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"_about": about, **res}, indent=1, sort_keys=True))
        ai = m.account_info()
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(json.dumps({"utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                                "login": getattr(ai, "login", None), "days": a.days,
                                "symbols": len(res)}) + "\n")
    print(json.dumps(res, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
