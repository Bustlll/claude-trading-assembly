"""BUILD SPECS (M5 clock): the DeltaForce market data as the replay's hard facts, M5 base.

Mirrors the project's own trainer (offline_trainer_ooslabXspeed/OOSLAB_DESIGN.md): M5 is the native
cache; every other timeframe is DERIVED from it by resampling (paper.py resample_tf), not downloaded
separately. Run by the operator.

Sources (read-only, in ~/deltaForceFTMO):
  data/study_bars/manifest.json                    per-symbol MT5 facts: digits, point, contract size,
                                                  tick size, tick value, spread snapshot, volume min/step
  data/accounts/*/symbol_registry.json (newest)  cross-check specs probed from MT5
  data/study_bars/<SYM>_M5.npy                    five-minute bars, broker clock (46 symbols)

Writes:
  guard/instruments.json        what the paper broker and the guard use (guard file: hash-protected)
  knowledge/symbols.json        the catalogue: specs, bar coverage, caveats
  knowledge/sessions.json       session and clock facts quoted from DeltaForce source (file:line)
  feed/<SYM>_M5.csv             one replay feed per symbol, with a session tag on each bar (day / night)
"""
import csv
import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HOME = Path(__file__).resolve().parents[1]
DF = Path.home().joinpath("deltaForceFTMO")
PRAGUE = ZoneInfo("Europe/Prague")
# Same seasonal rule as guard.rules.broker_to_utc (duplicated: this script runs before guard/instruments.json
# exists, so it cannot import guard.rules). Broker clock = NY local time + 7h: +2h from true UTC in NY winter,
# +3h in NY summer. Verified against HistData's own DST-aware build and against this project's MT5 data directly.
_NY = ZoneInfo("America/New_York")


def broker_to_utc(t_broker: int) -> datetime:
    guess = datetime.fromtimestamp(t_broker - 2 * 3600, tz=timezone.utc)
    offset_h = 3 if guess.astimezone(_NY).dst() else 2
    return datetime.fromtimestamp(t_broker - offset_h * 3600, tz=timezone.utc)


def asset_class(sym: str) -> str:
    if sym.endswith(".cash"):
        return "index"
    if sym.startswith(("XAU", "XAG", "XPT", "XPD", "XCU")):
        return "metal"
    if sym.startswith(("BTC", "ETH", "LTC", "BCH", "XRP")):
        return "crypto"
    if sym.endswith("OIL.cash"):
        return "energy"
    return "fx"


def load_manifest() -> dict:
    return json.loads((DF / "data/study_bars/manifest.json").read_text())


def newest_registry() -> dict:
    paths = sorted(glob.glob(str(DF / "data/accounts/*/symbol_registry.json")), key=os.path.getmtime)
    return json.loads(Path(paths[-1]).read_text())["specs"] if paths else {}


def spec_from_manifest(sym: str, m: dict) -> dict:
    point = float(m["point"])
    tick_size = float(m["trade_tick_size"])
    tick_value = float(m["trade_tick_value"])
    return {
        "symbol": sym, "class": asset_class(sym), "digits": int(m["digits"]), "point": point,
        "contract_size": float(m["trade_contract_size"]), "tick_size": tick_size,
        "tick_value_usd": tick_value,
        "usd_per_price_unit_per_lot": tick_value / tick_size,
        "spread_points_snapshot": float(m["spread"]), "spread_price_snapshot": float(m["spread"]) * point,
        "volume_min": float(m["volume_min"]), "volume_step": float(m["volume_step"]),
    }


MEASURED_SPREADS = HOME / "knowledge" / "measured_spreads.json"   # tools/measure_spreads.py --write


def spec_from_registry(sym: str, r: dict) -> dict:
    """Fallback for a symbol the manifest does not cover but the broker registry probed directly. The
    registry has no spread, which left these at 0 -- no spread cost in the paper replay -- until
    2026-10-13: the spread is now the MEASURED median of the broker's own M5 bar spread
    (knowledge/measured_spreads.json), 0 only if that file has nothing for the symbol."""
    pip = float(r["pip_size"])
    pip_value = float(r["pip_value_per_lot"])
    point = pip / 10
    try:
        median = float((json.loads(MEASURED_SPREADS.read_text()).get(sym) or {}).get("median") or 0.0)
    except (OSError, ValueError):
        median = 0.0
    return {
        "symbol": sym, "class": asset_class(sym), "digits": int(r.get("digits", 5)), "point": point,
        "contract_size": float(r.get("contract_size", 100_000)), "tick_size": pip, "tick_value_usd": pip_value,
        "usd_per_price_unit_per_lot": pip_value / pip,
        "spread_points_snapshot": round(median / point, 2) if median else 0.0, "spread_price_snapshot": median,
        "spread_source": ("measured median of the broker's M5 bar spread (tools/measure_spreads.py)" if median
                          else "none: no manifest snapshot and no measurement"),
        "volume_min": float(r["volume_min"]), "volume_step": float(r["volume_step"]),
        "registry_check": "spec sourced from the registry directly (not in the manifest)",
    }


def write_feed(sym: str) -> dict:
    a = np.load(DF / f"data/study_bars/{sym}_M5.npy")
    out = HOME / "feed" / f"{sym}_M5.csv"
    sessions = []
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_broker", "o", "h", "l", "c", "session"])
        for r in a:
            t = int(r["t_broker"])
            utc = broker_to_utc(t)
            hour = utc.astimezone(PRAGUE).hour
            night = "night" if (hour >= 22 or hour < 7) else "day"
            sessions.append(night)
            w.writerow([t, float(r["o"]), float(r["h"]), float(r["l"]), float(r["c"]), night])
    # a binary cache alongside the CSV: paper.py reads this (fast) instead of re-parsing the CSV text
    # every time a command runs. The CSV stays the human-readable source of truth.
    cache_dtype = np.dtype([("t", "i8"), ("o", "f8"), ("h", "f8"), ("l", "f8"), ("c", "f8"), ("session", "U5")])
    cache = np.empty(len(a), dtype=cache_dtype)
    cache["t"] = a["t_broker"]
    cache["o"], cache["h"], cache["l"], cache["c"] = a["o"], a["h"], a["l"], a["c"]
    cache["session"] = sessions
    np.save(HOME / "feed" / f"{sym}_M5.npy", cache)
    nights = sum(1 for s_ in sessions if s_ == "night")
    return {"bars": len(a), "night_bars": nights, "first_t": int(a[0]["t_broker"]), "last_t": int(a[-1]["t_broker"])}


SESSIONS = {
    "source": "deltaforce/live/ftmo_session.py (header comments and constants), memory notes, OOSLAB_DESIGN.md",
    "base_clock": "M5 (five-minute bars), broker server time, UTC+3. Mirrors the project's own 5m cache "
                  "(offline_trainer_ooslabXspeed/OOSLAB_DESIGN.md). H1/H4/D1 are DERIVED by resampling, not downloaded.",
    "friday_force_close": "Friday >= 21:00 UTC (ftmo_session.py:7). The replay flattens every open position on the "
                          "last bar before a weekend gap (no position holds over the weekend; operator's rule).",
    "swap_window_utc": "21:55-22:10 UTC is the guarded swap-spike window (ftmo_session.py:27)",
    "server_midnight_rollover_utc": "20:55-21:10 UTC is the real server-midnight rollover (ftmo_session.py:27)",
    "daily_reset": "FTMO daily limit resets 00:00 CE(S)T (field/ftmo_rules_gaps.md, settled from ftmo.com)",
    "broker_clock": "feed stamps are broker server time, UTC+3 (field/candles_and_clock.md)",
    "night_tag": "bars tagged 'night' are 22:00-07:00 Prague local time. Trading at night is enabled (operator's rule).",
    "news": "DeltaForce has a calendar daemon (deltaforce/live/ftmo_calendar_daemon.py). Replay has no news feed.",
    "weekends": "No weekend trading: the broker flattens before every weekend gap, and windows are built from "
                "business days only (Mon-Fri), so a window never includes a weekend.",
}


def main() -> int:
    m = load_manifest()
    reg = newest_registry()
    instruments, catalogue = {}, {}
    m5_symbols = sorted(os.path.basename(p)[:-7] for p in glob.glob(str(DF / "data/study_bars/*_M5.npy")))
    for sym in m5_symbols:
        if sym in m:
            spec = spec_from_manifest(sym, m[sym])
            cross = reg.get(sym)
            if cross is None:
                spec["registry_check"] = "no registry entry"
            else:
                mine = spec["usd_per_price_unit_per_lot"] * cross["pip_size"]
                theirs = float(cross["pip_value_per_lot"])
                spec["registry_check"] = ("agrees" if abs(mine - theirs) <= 1e-3 * max(1.0, abs(theirs))
                                          else f"differs: manifest {mine:.6f} vs registry {theirs:.6f} USD/pip/lot")
        elif sym in reg:
            spec = spec_from_registry(sym, reg[sym])
        else:
            continue  # no spec from either source: not tradeable, listed in specs_without_bars' mirror below
        feed = write_feed(sym)
        instruments[sym] = {**spec, "commission_rt_per_lot": 5.0}  # FTMO forex $2.50/side (field/instruments_costs.md)
        catalogue[sym] = {"spec": spec, "feed": feed, "caveats": [c for c in [
            ("spread is the manifest snapshot, not a history: FX spread in the bar file is ~0 and unusable"
             if "spread_source" not in spec else f"spread: {spec['spread_source']}"),
            "non-FX commission is an assumption" if spec["class"] != "fx" else None,
            "no registry cross-check" if cross is None else None,
            f"registry: {spec['registry_check']}"] if c]}
    no_spec_with_bars = sorted(set(m5_symbols) - set(instruments))
    no_bars = sorted(s for s in reg if s not in instruments and s not in no_spec_with_bars)
    (HOME / "guard" / "instruments.json").write_text(json.dumps(instruments, indent=1, sort_keys=True))
    (HOME / "knowledge").mkdir(exist_ok=True)
    (HOME / "knowledge" / "symbols.json").write_text(json.dumps({
        "built": datetime.now(timezone.utc).isoformat(timespec="seconds"), "base_clock": "M5",
        "replayable": sorted(instruments), "specs_without_bars": no_bars,
        "bars_without_spec": no_spec_with_bars, "symbols": catalogue}, indent=1, sort_keys=True))
    (HOME / "knowledge" / "sessions.json").write_text(json.dumps(SESSIONS, indent=1))
    print(json.dumps({"replayable": len(instruments), "specs_without_bars": len(no_bars)}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
