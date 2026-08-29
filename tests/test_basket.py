"""Tests for running one strategy across several markets as a portfolio.

Diversification is the only improvement in finance that requires predicting nothing,
and it is also the easiest to claim without earning. Two traps, both of which this
package walked into while the module was being written and both of which are pinned
here:

* **Comparing windows that are not the same window.** A single market handed to a
  basket of one is not trimmed by anything, so it quietly gets measured over ten years
  while the basket it is "compared" against gets the eighteen months its members share.
  That produced a 313% versus 40% headline that meant nothing at all.

* **Comparing against the wrong benchmark.** A basket beating one of its own members
  shows only that the member was bad. The test that means something is the basket
  against an equal-weight hold of *the same basket* - and that is the number that says
  whether the strategy did anything.
"""

from __future__ import annotations

import unittest

from tradebot import basket
from tradebot.costs import CostModel
from tradebot.engine import ExecutionSettings
from tradebot.feeds.synthetic import SyntheticFeed
from tradebot.risk import RiskLimits
from tradebot.types import Candle

DAY = 86_400_000
COSTS = CostModel(taker_fee_bps=7.5, maker_fee_bps=7.5, half_spread_bps=1, slippage_bps=2)
LIMITS = RiskLimits(max_position_pct=1.0, max_daily_loss_pct=0.99, max_drawdown_pct=0.99,
                    max_trades_per_day=10_000, min_trade_notional=1.0, cooldown_bars_after_loss=0)
EXEC = ExecutionSettings(min_notional=1.0)


def daily(prices, start_day=0):
    return [
        Candle(ts=(start_day + i) * DAY, open=p, high=p * 1.01, low=p * 0.99, close=p, volume=1.0)
        for i, p in enumerate(map(float, prices))
    ]


def run(series, strategy="buy_and_hold", **kw):
    return basket.run(series, strategy, starting_cash=10_000.0,
                      costs=COSTS, limits=LIMITS, execution=EXEC, **kw)


class TestAlignment(unittest.TestCase):
    def test_it_trims_to_the_shared_window(self):
        a = daily([100] * 10, start_day=0)
        b = daily([50] * 10, start_day=5)
        out = basket.align({"A": a, "B": b})
        self.assertEqual(len(out["A"]), 5)
        self.assertEqual(len(out["B"]), 5)
        self.assertEqual([c.ts for c in out["A"]], [c.ts for c in out["B"]])

    def test_markets_that_never_overlap_are_refused(self):
        with self.assertRaises(ValueError):
            basket.align({"A": daily([100] * 5, 0), "B": daily([100] * 5, 900)})

    def test_an_empty_basket_is_refused(self):
        with self.assertRaises(ValueError):
            basket.align({})

    def test_every_member_is_measured_over_the_same_bars(self):
        """The mistake that produced a meaningless 313%-versus-40% comparison."""
        a, b = daily([100] * 40, 0), daily([100] * 12, 28)
        out = basket.align({"A": a, "B": b})
        spans = {(bars[0].ts, bars[-1].ts) for bars in out.values()}
        self.assertEqual(len(spans), 1, "members ended up on different windows")


class TestDailyRollUp(unittest.TestCase):
    def test_hourly_bars_become_one_bar_a_day(self):
        hours = [Candle(ts=h * 3_600_000, open=100 + h, high=110 + h, low=90 + h,
                        close=105 + h, volume=1.0) for h in range(48)]
        out = basket.to_daily(hours)
        self.assertEqual(len(out), 2)

    def test_the_day_keeps_the_first_open_and_last_close(self):
        hours = [Candle(ts=h * 3_600_000, open=100 + h, high=200, low=50,
                        close=105 + h, volume=1.0) for h in range(24)]
        day = basket.to_daily(hours)[0]
        self.assertAlmostEqual(day.open, 100.0)
        self.assertAlmostEqual(day.close, 105.0 + 23)

    def test_the_day_keeps_the_true_high_and_low(self):
        """The extremes must be the day's, wherever in the day they happened.

        The fixture puts both in the middle on purpose: with a monotonic ramp, code
        that simply keeps the last bar's values gets the right answer by accident and
        the test proves nothing.
        """
        highs = [100, 105, 130, 108, 102, 101]      # peak at hour 2
        lows = [99, 96, 95, 70, 94, 98]             # trough at hour 3
        hours = [Candle(ts=h * 3_600_000, open=100, high=hi, low=lo, close=100, volume=1.0)
                 for h, (hi, lo) in enumerate(zip(highs, lows))]
        day = basket.to_daily(hours)[0]
        self.assertAlmostEqual(day.high, 130.0)
        self.assertAlmostEqual(day.low, 70.0)
        self.assertNotAlmostEqual(day.low, lows[-1], msg="kept the last low, not the lowest")
        self.assertNotAlmostEqual(day.high, highs[-1], msg="kept the last high, not the highest")

    def test_volume_is_summed(self):
        hours = [Candle(ts=h * 3_600_000, open=1, high=1, low=1, close=1, volume=2.0)
                 for h in range(24)]
        self.assertAlmostEqual(basket.to_daily(hours)[0].volume, 48.0)


class TestCorrelation(unittest.TestCase):
    """What decides whether a basket diversifies at all."""

    def test_a_market_against_itself_is_one(self):
        bars = SyntheticFeed(bars=200, seed=4, interval_ms=DAY).generate()
        c = basket.correlation({"A": bars, "B": bars})
        self.assertAlmostEqual(c[("A", "B")], 1.0, places=6)

    def test_a_mirror_image_is_minus_one(self):
        bars = SyntheticFeed(bars=200, seed=4, interval_ms=DAY).generate()
        mirror = []
        base = bars[0].close
        for c in bars:
            p = base * (2.0 - c.close / base)
            mirror.append(Candle(ts=c.ts, open=p, high=p * 1.01, low=p * 0.99, close=p, volume=1.0))
        value = basket.correlation({"A": bars, "B": mirror})[("A", "B")]
        self.assertLess(value, -0.9)

    def test_unrelated_markets_score_near_zero(self):
        a = SyntheticFeed(bars=400, seed=1, interval_ms=DAY).generate()
        b = SyntheticFeed(bars=400, seed=99, interval_ms=DAY).generate()
        self.assertLess(abs(basket.correlation({"A": a, "B": b})[("A", "B")]), 0.25)


class TestTheBasketAddsUp(unittest.TestCase):
    def test_capital_is_split_equally(self):
        flat = {name: daily([100] * 30) for name in ("A", "B", "C", "D")}
        result = run(flat)
        self.assertAlmostEqual(result.benchmark.starting_equity, 10_000.0)

    def test_the_combined_curve_is_the_sum_of_its_parts(self):
        """Checked as arithmetic, not as a plausible-looking number.

        Two markets that behave differently, so a curve built from one sleeve and
        multiplied up cannot pass by coincidence.
        """
        from tradebot import backtest as backtest_mod
        from tradebot.strategies import build

        series = {"A": daily([100 + i for i in range(60)]),
                  "B": daily([200 - i * 0.5 for i in range(60)])}
        result = run(series)

        sleeves = {
            name: backtest_mod.run(bars, build("vol_target"), starting_cash=5_000.0,
                                   costs=COSTS, limits=LIMITS, execution=EXEC)
            for name, bars in series.items()
        }
        held = {
            name: backtest_mod.run(bars, build("buy_and_hold"), starting_cash=5_000.0,
                                   costs=COSTS, limits=LIMITS, execution=EXEC)
            for name, bars in series.items()
        }
        curves = [r.engine.portfolio.equity_curve for r in held.values()]
        self.assertEqual(len(result.curve), 60)
        for i in (0, 17, 42, 59):
            expected = sum(c[i].equity for c in curves)
            self.assertAlmostEqual(result.curve[i].equity, expected, places=6,
                                   msg=f"bar {i} is not the sum of the sleeves")
        self.assertNotEqual(sleeves["A"].metrics.ending_equity,
                            sleeves["B"].metrics.ending_equity,
                            "the fixture's two markets are too alike to catch a bad sum")

    def test_two_identical_markets_behave_like_one(self):
        """A basket of clones diversifies nothing - the maths must say so."""
        bars = SyntheticFeed(bars=400, seed=7, interval_ms=DAY).generate()
        alone = run({"A": bars})
        cloned = run({"A": bars, "B": bars})
        self.assertAlmostEqual(alone.benchmark.total_return_pct,
                               cloned.benchmark.total_return_pct, places=4)
        self.assertAlmostEqual(alone.benchmark.max_drawdown_pct,
                               cloned.benchmark.max_drawdown_pct, places=4)

    def test_uncorrelated_markets_cut_the_drawdown(self):
        """The entire point: offsetting wobbles produce a shallower worst case."""
        a = SyntheticFeed(bars=800, seed=11, interval_ms=DAY, drift_per_bar=0.0004).generate()
        b = SyntheticFeed(bars=800, seed=77, interval_ms=DAY, drift_per_bar=0.0004).generate()
        solo = max(run({"A": a}).benchmark.max_drawdown_pct,
                   run({"B": b}).benchmark.max_drawdown_pct)
        together = run({"A": a, "B": b}).benchmark.max_drawdown_pct
        self.assertLess(together, solo,
                        "holding two unrelated markets was not calmer than the worse one alone")


class TestItIsJudgedAgainstTheRightThing(unittest.TestCase):
    def test_the_benchmark_is_the_same_basket_held(self):
        series = {"A": SyntheticFeed(bars=400, seed=3, interval_ms=DAY).generate(),
                  "B": SyntheticFeed(bars=400, seed=8, interval_ms=DAY).generate()}
        held = run(series, "buy_and_hold")
        active = run(series, "vol_target")
        self.assertAlmostEqual(active.benchmark.total_return_pct,
                               held.benchmark.total_return_pct, places=6)

    def test_the_gap_is_measured_against_that_benchmark(self):
        series = {"A": SyntheticFeed(bars=400, seed=3, interval_ms=DAY).generate(),
                  "B": SyntheticFeed(bars=400, seed=8, interval_ms=DAY).generate()}
        r = run(series, "vol_target")
        self.assertAlmostEqual(
            r.gap, r.metrics.total_return_pct - r.benchmark.total_return_pct, places=9)

    def test_holding_the_basket_scores_a_gap_of_about_zero(self):
        """Buy-and-hold against buy-and-hold has to come out even."""
        series = {"A": SyntheticFeed(bars=400, seed=3, interval_ms=DAY).generate(),
                  "B": SyntheticFeed(bars=400, seed=8, interval_ms=DAY).generate()}
        self.assertAlmostEqual(run(series, "buy_and_hold").gap, 0.0, places=6)

    def test_the_report_names_both_benchmarks(self):
        series = {"A": SyntheticFeed(bars=400, seed=3, interval_ms=DAY).generate(),
                  "B": SyntheticFeed(bars=400, seed=8, interval_ms=DAY).generate()}
        text = basket.render(run(series, "vol_target"))
        self.assertIn("hold the same basket", text)
        self.assertIn("best single member", text)

    def test_every_member_gets_its_own_line(self):
        series = {"A": SyntheticFeed(bars=300, seed=3, interval_ms=DAY).generate(),
                  "B": SyntheticFeed(bars=300, seed=8, interval_ms=DAY).generate(),
                  "C": SyntheticFeed(bars=300, seed=12, interval_ms=DAY).generate()}
        r = run(series, "vol_target")
        self.assertEqual(sorted(r.per_symbol), ["A", "B", "C"])
        self.assertEqual(r.symbols, ["A", "B", "C"])


if __name__ == "__main__":
    unittest.main()
