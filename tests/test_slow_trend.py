"""Tests for the low-frequency trend filter.

Its whole claim is trading rarely enough that costs stop mattering, so the trade
count and the band that suppresses whipsaw matter as much as the entry logic.
"""

from __future__ import annotations

import unittest

from tradebot import backtest
from tradebot.costs import CostModel
from tradebot.engine import ExecutionSettings
from tradebot.feeds.synthetic import SyntheticFeed
from tradebot.risk import RiskLimits
from tradebot.strategies import build
from tradebot.strategies.base import Context
from tradebot.types import Candle

DAY = 86_400_000


def candle(day, close):
    return Candle(ts=day * DAY, open=close, high=close * 1.001,
                  low=close * 0.999, close=close, volume=1.0)


class TestSlowTrendLogic(unittest.TestCase):
    def _warm(self, strategy, price=100.0, bars=60):
        ctx = Context(exposure=0.0, equity=1000.0, costs=CostModel())
        for i in range(bars):
            strategy.on_candle(candle(i, price), ctx)
        return ctx

    def test_it_holds_when_price_is_above_the_average(self):
        s = build("slow_trend", period=20, band_pct=0.02)
        ctx = self._warm(s)
        d = s.on_candle(candle(99, 130.0), ctx)
        self.assertAlmostEqual(d.target_weight, 1.0)
        self.assertIn("above trend", d.reason)

    def test_it_goes_to_cash_when_price_is_below_the_average(self):
        s = build("slow_trend", period=20, band_pct=0.02)
        ctx = self._warm(s)
        d = s.on_candle(candle(99, 70.0), ctx)
        self.assertAlmostEqual(d.target_weight, 0.0)
        self.assertIn("below trend", d.reason)

    def test_inside_the_band_it_changes_nothing(self):
        """The band is what stops price hovering on the line racking up costs."""
        s = build("slow_trend", period=20, band_pct=0.05)
        ctx = self._warm(s)
        d = s.on_candle(candle(99, 100.5), ctx)
        self.assertTrue(d.is_hold, "must hold, not re-target, inside the band")

    def test_it_reports_nothing_until_warm(self):
        s = build("slow_trend", period=50)
        ctx = Context(exposure=0.0, equity=1000.0, costs=CostModel())
        d = s.on_candle(candle(1, 100.0), ctx)
        self.assertAlmostEqual(d.target_weight, 0.0)
        self.assertIn("warming up", d.reason)

    def test_bad_parameters_are_rejected(self):
        for bad in ({"period": 1}, {"band_pct": -0.1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                build("slow_trend", **bad)


class TestSlowTrendCosts(unittest.TestCase):
    def _run(self, name, candles):
        return backtest.run(
            candles, build(name), starting_cash=10_000.0,
            costs=CostModel(taker_fee_bps=7.5, maker_fee_bps=7.5,
                            half_spread_bps=1, slippage_bps=2),
            limits=RiskLimits(max_position_pct=1.0, max_daily_loss_pct=0.99,
                              max_drawdown_pct=0.99, max_trades_per_day=10_000,
                              min_trade_notional=1.0, cooldown_bars_after_loss=0),
            execution=ExecutionSettings(min_notional=1.0),
        )

    def setUp(self):
        self.candles = SyntheticFeed(bars=4000, seed=17).generate()

    def test_it_trades_far_less_than_the_fast_strategies(self):
        """The entire point: rare enough that cost drag stops deciding the outcome."""
        slow = self._run("slow_trend", self.candles)
        fast = self._run("micro_scalp", self.candles)
        self.assertLess(slow.metrics.trades, fast.metrics.trades / 10)

    def test_its_cost_drag_stays_low(self):
        slow = self._run("slow_trend", self.candles)
        fast = self._run("micro_scalp", self.candles)
        self.assertLess(slow.metrics.cost_drag_pct, fast.metrics.cost_drag_pct / 5)

    def test_a_wider_band_means_fewer_trades(self):
        tight = backtest.run(self.candles, build("slow_trend", period=100, band_pct=0.0),
                             starting_cash=10_000.0)
        wide = backtest.run(self.candles, build("slow_trend", period=100, band_pct=0.10),
                            starting_cash=10_000.0)
        self.assertLess(wide.metrics.trades, tight.metrics.trades)


class TestCostDragMetric(unittest.TestCase):
    def test_drag_is_costs_over_starting_capital(self):
        candles = SyntheticFeed(bars=2000, seed=4).generate()
        result = backtest.run(candles, build("micro_scalp"), starting_cash=1_000.0)
        m = result.metrics
        self.assertAlmostEqual(m.cost_drag_pct, m.total_costs / 1_000.0 * 100.0, places=6)

    def test_annualised_drag_scales_by_duration(self):
        candles = SyntheticFeed(bars=2000, seed=4).generate()
        m = backtest.run(candles, build("micro_scalp"), starting_cash=1_000.0).metrics
        self.assertAlmostEqual(m.cost_drag_annual_pct, m.cost_drag_pct / m.years, places=4)

    def test_a_ruinous_drag_is_called_out_in_the_report(self):
        candles = SyntheticFeed(bars=3000, seed=8).generate()
        text = backtest.run(candles, build("micro_scalp"), starting_cash=1_000.0).metrics.render()
        self.assertIn("Cost drag", text)
        self.assertIn("No edge survives that", text)


if __name__ == "__main__":
    unittest.main()
