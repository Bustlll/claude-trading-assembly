"""EXTEND EURUSD HISTORY: splice in the cleaned 26-year HistData series before the MT5 data begins.

Operator-run, one-off. Not part of build_specs.py's main loop (different source, one symbol only).

Source: research276/storm/data/EURUSD_HD_M5.npz (HistData.com, cleaned of corrupt ticks, already
broker-clock converted by the SAME seasonal rule as guard.rules.broker_to_utc -- verified bit-identical
to this project's own MT5 EURUSD data at every overlapping timestamp, in both a winter and a summer
sample. See manual/HANDOFF.md section 4b.

This only deepens EURUSD's own readable history (bars/resample for research). It does NOT change the
replay's tradable windows: those need 10 symbols overlapping, still gated by the other 33 starting in
2024-2025. Purely additive, purely read-only context.
"""
import csv
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HOME = Path(__file__).resolve().parents[1]
HISTDATA = Path.home().joinpath("deltaForceFTMO/research276/storm/data/EURUSD_HD_M5.npz")
PRAGUE = ZoneInfo("Europe/Prague")
sys.path.insert(0, str(HOME))
from guard import rules as R  # noqa: E402


def main() -> int:
    feed_csv = HOME / "feed" / "EURUSD_M5.csv"
    feed_npy = HOME / "feed" / "EURUSD_M5.npy"
    if not feed_csv.exists():
        raise SystemExit("run tools/build_specs.py first")

    if feed_npy.exists():
        boundary = int(np.load(feed_npy)["t"].min())
    else:
        with open(feed_csv) as f:
            boundary = int(next(csv.DictReader(f))["t_broker"])

    hd = np.load(HISTDATA)
    hd_t = hd["t"].astype("int64")
    keep = hd_t < boundary  # strictly before the MT5 data: no overlap, no duplicate timestamps
    n_hist = int(keep.sum())
    if n_hist == 0:
        print("nothing before the boundary; already at max, or the boundary is wrong")
        return 1

    sessions = []
    for t in hd_t[keep]:
        hour = R.broker_to_utc(int(t)).astimezone(PRAGUE).hour
        sessions.append("night" if (hour >= 22 or hour < 7) else "day")
    hist_rows = list(zip(hd_t[keep].tolist(), hd["o"][keep].tolist(), hd["h"][keep].tolist(),
                         hd["l"][keep].tolist(), hd["c"][keep].tolist(), sessions))

    with open(feed_csv) as f:
        existing_lines = f.read().splitlines()
    header, existing_rows = existing_lines[0], existing_lines[1:]

    tmp = feed_csv.with_suffix(".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header.split(","))
        for t, o, h, l, c, s in hist_rows:
            w.writerow([t, o, h, l, c, s])
        for line in existing_rows:
            f.write(line + "\n")
    tmp.replace(feed_csv)

    # rebuild the binary cache the same way build_specs.py does, so paper.py's fast path picks it up
    with open(feed_csv) as f:
        rows = list(csv.DictReader(f))
    cache_dtype = np.dtype([("t", "i8"), ("o", "f8"), ("h", "f8"), ("l", "f8"), ("c", "f8"), ("session", "U5")])
    cache = np.empty(len(rows), dtype=cache_dtype)
    cache["t"] = [int(r["t_broker"]) for r in rows]
    cache["o"] = [float(r["o"]) for r in rows]
    cache["h"] = [float(r["h"]) for r in rows]
    cache["l"] = [float(r["l"]) for r in rows]
    cache["c"] = [float(r["c"]) for r in rows]
    cache["session"] = [r["session"] for r in rows]
    np.save(feed_npy, cache)

    print(f"spliced {n_hist} HistData bars before {R.broker_to_utc(boundary).date()} "
          f"(from {R.broker_to_utc(int(hd_t[keep].min())).date()}); total EURUSD M5 bars now {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
