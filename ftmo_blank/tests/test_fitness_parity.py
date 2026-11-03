"""Tests for business days, fitness measurements, the multi-symbol broker, and parity with the DeltaForce dashboard."""
import ast
import csv
import random
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "tools"))
sys.path.insert(0, str(HOME))
import json  # noqa: E402
import paper as P  # noqa: E402
sys.path.insert(0, str(HOME / "tools"))
import importlib.util as _ilu  # noqa: E402
_spec = _ilu.spec_from_file_location("operator_tool", HOME / "tools" / "operator.py")
operator_tool = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(operator_tool)
from guard import rules as R  # noqa: E402

def rows(path, kind=None):
    with open(path) as f:
        out = [json.loads(line) for line in f]
    return [r for r in out if kind is None or r["type"] == kind]


DASH = Path.home().joinpath("deltaForceFTMO/deltaforce/dashboard/server.py")


def dashboard_r():
    """Compile the DeltaForce dashboard's own _chart_r_multiple from its source, so parity is checked, not assumed."""
    tree = ast.parse(DASH.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_chart_r_multiple")
    mod = ast.Module(body=[fn], type_ignores=[])
    import typing
    ns = {"Optional": typing.Optional, "Any": typing.Any}
    exec(compile(mod, str(DASH), "exec"), ns)
    return ns["_chart_r_multiple"]


class BusinessDayTest(unittest.TestCase):
    def test_monday_to_sunday_is_five(self):
        self.assertEqual(P.business_days_between("2026-01-05", "2026-01-11"), 5)

    def test_weekend_only_is_zero(self):
        self.assertEqual(P.business_days_between("2026-01-10", "2026-01-11"), 0)


class FitnessTest(unittest.TestCase):
    def close(self, day, r, result, bal):
        return {"type": "trade_closed", "closed_utc": f"{day} 12:00 UTC", "r_price": r, "r_net": r * 0.9,
                "result": result, "balance": bal}

    def test_measures_on_a_known_set(self):
        closes = [self.close("2026-01-05", 1.0, "win", 10100),
                  self.close("2026-01-05", -1.0, "loss", 10000)]
        f = P.fitness_components(closes, "2026-01-05", "2026-01-11", 10000)
        self.assertEqual(f["closed_trades"], 2)
        self.assertEqual(f["business_days_elapsed"], 5)
        self.assertEqual(f["mean_r_price"], 0.0)
        self.assertEqual(f["win_rate"], 0.5)
        self.assertEqual(f["max_drawdown_pct"], round(100 * 100 / 10100, 2))
        self.assertEqual(f["longest_stall_business_days"], 4)  # Tue..Fri with no close after Mon

    def test_no_trades_gives_no_score(self):
        f = P.fitness_components([], "2026-01-05", "2026-01-11", 10000)
        self.assertIsNone(f["score_v0_hypothesis"])
        self.assertEqual(f["longest_stall_business_days"], 5)


class ParityTest(unittest.TestCase):
    def test_r_matches_the_dashboard_function_exactly(self):
        dash = dashboard_r()
        rng = random.Random(7)
        for _ in range(500):
            side = rng.choice(["long", "short"])
            entry = rng.uniform(1.0, 1.3)
            sl = entry + rng.uniform(-0.01, 0.01) if rng.random() < .9 else None
            exit_px = entry + rng.uniform(-0.02, 0.02)
            self.assertEqual(dash(side, entry, exit_px, sl), R.chart_r_multiple(side, entry, exit_px, sl))

    def test_missing_stop_is_none_in_both(self):
        self.assertIsNone(dashboard_r()("long", 1.1, 1.2, None))
        self.assertIsNone(R.chart_r_multiple("long", 1.1, 1.2, None))


class MultiSymbolTest(unittest.TestCase):
    def home(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "feed").mkdir()
        (tmp / "state").mkdir()
        t0 = 1767222000  # 2026-01-01 00:00 broker clock
        for sym, px in (("EURUSD", 1.1), ("XAUUSD", 2000.0)):
            with open(tmp / "feed" / f"{sym}_M5.csv", "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["t_broker", "o", "h", "l", "c", "session"])
                for i in range(10):
                    w.writerow([t0 + i * 3600, px, px * 1.001, px * 0.999, px, "day"])
        return P.Paper(home=tmp)

    def test_gold_risk_uses_gold_tick_value_not_fx(self):
        p = self.home()
        p.advance(3)
        # 0.07 lots, not 0.10: at 0.10 this risks ~$92.60, over the current 0.7%/$70 default per-trade
        # cap (guard/rules.py RISK_FACTOR_DEFAULT) -- the cap itself is what this test is not about.
        r = p.place_order("XAUUSD", "buy", 0.07, sl=1991.0, tp=None, why="gold risk unit check here")
        self.assertTrue(r["ok"], r)
        p.advance(1)
        pos = p.s["positions"][0]
        self.assertEqual(pos["symbol"], "XAUUSD")
        self.assertAlmostEqual(pos["risk_usd"], (pos["entry"] - 1991.0) * 0.07 * 100, places=1)

    def test_unreplayable_symbol_is_refused(self):
        p = self.home()
        p.advance(3)
        r = p.place_order("AMD", "buy", 0.10, sl=1.0, tp=None, why="a symbol with no bars here")
        self.assertFalse(r["ok"])
        self.assertIn("not replayable", r["reason"])


if __name__ == "__main__":
    unittest.main()


def make_business_days(symbol, day_bars, deadline=None):
    """day_bars: list of lists of (o,h,l,c), one list per CONSECUTIVE BUSINESS DAY (weekends are not in
    the data at all, like a real FX feed). Returns a Paper on a temp home."""
    tmp = Path(tempfile.mkdtemp())
    (tmp / "feed").mkdir()
    (tmp / "state").mkdir()
    d = date(2026, 1, 5)  # a Monday
    rows_out = []
    for bars in day_bars:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        t0 = int(datetime(d.year, d.month, d.day, 10, tzinfo=timezone.utc).timestamp()) + (3 if 3 <= d.month <= 10 else 2) * 3600  # rough NY-DST season guess, fine for these fixed test dates
        for i, (o, h, l, c) in enumerate(bars):
            rows_out.append((t0 + i * 3600, o, h, l, c))
        d += timedelta(days=1)
    with open(tmp / "feed" / f"{symbol}_M5.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_broker", "o", "h", "l", "c", "session"])
        for t, o, h, l, c in rows_out:
            w.writerow([t, o, h, l, c, "day"])
    if deadline is None:
        return P.Paper(home=tmp)
    old = R.CHALLENGE["deadline_business_days"]
    R.CHALLENGE["deadline_business_days"] = deadline
    try:
        return P.Paper(home=tmp)
    finally:
        R.CHALLENGE["deadline_business_days"] = old


FLAT = (1.1000, 1.1002, 1.0998, 1.1000)
FLAT3 = [FLAT] * 3


class ResampleTest(unittest.TestCase):
    def bars(self, n, t0=1_700_000_000 - (1_700_000_000 % 3600)):
        return [{"t": t0 + i * 300, "o": 1.0 + i * 0.0001, "h": 1.0 + i * 0.0001 + 0.0002,
                 "l": 1.0 + i * 0.0001 - 0.0001, "c": 1.0 + i * 0.0001 + 0.00005, "session": "day"} for i in range(n)]

    def test_h1_bucket_aggregates_twelve_m5_bars_correctly(self):
        bars = self.bars(24)  # exactly two H1 buckets' worth
        out = P.resample(bars, "H1", upto_t=bars[-1]["t"] + 300)  # upto_t past the last bar: both buckets complete
        self.assertEqual(len(out), 2)
        first12 = bars[:12]
        self.assertEqual(out[0]["o"], first12[0]["o"])
        self.assertEqual(out[0]["c"], first12[-1]["c"])
        self.assertAlmostEqual(out[0]["h"], max(b["h"] for b in first12))
        self.assertAlmostEqual(out[0]["l"], min(b["l"] for b in first12))

    def test_the_forming_bucket_is_never_shown(self):
        bars = self.bars(18)  # one and a half H1 buckets
        out = P.resample(bars, "H1", upto_t=bars[-1]["t"])
        self.assertEqual(len(out), 1)  # the partial second bucket is excluded

    def test_m5_passthrough_respects_upto_t(self):
        bars = self.bars(5)
        out = P.resample(bars, "M5", upto_t=bars[2]["t"])
        self.assertEqual(len(out), 3)


class WindowAndWeekendTest(unittest.TestCase):
    def test_windows_partition_business_days_only(self):
        p = make_business_days("EURUSD", [FLAT3] * 6, deadline=2)
        self.assertEqual(len(p.windows), 3)  # 6 business days / 2 per window

    def test_position_is_flattened_before_the_weekend_not_held_over_it(self):
        # Mon..Fri then the following Monday, with nothing on the weekend (like a real FX feed)
        p = make_business_days("EURUSD", [FLAT3] * 6, deadline=10)
        p.advance(3)
        self.assertTrue(p.place_order("EURUSD", "buy", 0.10, sl=1.0950, tp=None, why="held into the weekend test")["ok"])
        p.advance(2)  # fill, still Monday
        # advance through the remaining days up to and past the Friday->Monday boundary
        p.advance(200)
        flats = rows(p.ledger_path, "trade_closed")
        self.assertTrue(any(c["reason"] == "weekend_flatten" for c in flats))


class PassRateMilestoneTest(unittest.TestCase):
    def test_each_milestone_fires_once_the_first_time_it_is_crossed(self):
        p = make_business_days("EURUSD", [FLAT3], deadline=1)
        # 1 pass out of 1 = 100%: every milestone up to 0.95 should fire, once each
        p._end_window("passed")
        fired = [r["milestone"] for r in rows(p.ledger_path, "pass_rate_milestone")]
        self.assertEqual(fired, R.PASS_RATE_MILESTONES)
        self.assertEqual(p.s["milestones_fired"], R.PASS_RATE_MILESTONES)
        # ending another window at the same rate must not refire anything
        p._end_window("passed")
        fired_again = rows(p.ledger_path, "pass_rate_milestone")
        self.assertEqual(len(fired_again), len(R.PASS_RATE_MILESTONES))

    def test_a_pass_logs_attempt_passed_review_and_counts_toward_the_rate(self):
        p = make_business_days("EURUSD", [FLAT3], deadline=1)
        p._end_window("window_end")
        p._end_window("passed")
        self.assertEqual(p.s["attempts_total"], 2)
        self.assertEqual(p.s["attempts_passed"], 1)
        self.assertTrue(rows(p.ledger_path, "attempt_passed_review"))


class EpochWraparoundTest(unittest.TestCase):
    def test_finishing_every_window_wraps_back_to_the_first_one(self):
        # 2 business days, 1 per window -> 2 windows in the epoch
        p = make_business_days("EURUSD", [FLAT3, FLAT3], deadline=1)
        self.assertEqual(len(p.windows), 2)
        p.advance(3)    # finish window 0 (day 1's 3 bars)
        self.assertEqual(p.s["epoch"], 0)
        self.assertEqual(p.s["window_index"], 1)
        p.advance(3)    # finish window 1 (day 2's 3 bars): the epoch ends and wraps
        self.assertEqual(p.s["epoch"], 1)
        self.assertEqual(p.s["window_index"], 0)
        self.assertTrue(rows(p.ledger_path, "epoch_review"))
        self.assertEqual(p.s["cursor"], p._index_of(p.windows[0][0]))  # back at the same start point
        p.advance(2)
        self.assertEqual(p.s["day"], "2026-01-05")  # day 1 of the data again, epoch 1



if __name__ == "__main__":
    unittest.main()
