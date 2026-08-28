"""Tests for the parameter sweep - the tool whose job is to expose cherry-picking."""

from __future__ import annotations

import unittest

from tradebot import sweep as sweep_mod
from tradebot.costs import CostModel
from tradebot.engine import ExecutionSettings
from tradebot.feeds.synthetic import SyntheticFeed
from tradebot.risk import RiskLimits


class TestSweep(unittest.TestCase):
    def setUp(self):
        self.series = {
            "A": SyntheticFeed(bars=1200, seed=1, drift_per_bar=0.0003).generate(),
            "B": SyntheticFeed(bars=1200, seed=2, drift_per_bar=-0.0002).generate(),
        }
        self.kwargs = dict(
            starting_cash=1000.0,
            costs=CostModel(taker_fee_bps=7.5, maker_fee_bps=7.5, half_spread_bps=1, slippage_bps=2),
            limits=RiskLimits(max_position_pct=1.0, max_daily_loss_pct=0.99,
                              max_drawdown_pct=0.99, max_trades_per_day=10_000,
                              min_trade_notional=1.0, cooldown_bars_after_loss=0),
            execution=ExecutionSettings(min_notional=1.0),
        )

    def test_it_runs_every_combination_on_every_series(self):
        result = sweep_mod.run(self.series, "slow_trend",
                               {"period": [50, 100], "band_pct": [0.0, 0.05]}, **self.kwargs)
        self.assertEqual(len(result.cells), 2 * 2 * 2, "2 periods x 2 bands x 2 series")
        self.assertEqual(len(result.settings()), 4)

    def test_each_cell_is_compared_to_holding_the_same_series(self):
        result = sweep_mod.run(self.series, "slow_trend", {"period": [100]}, **self.kwargs)
        for cell in result.cells:
            expected = cell.metrics.total_return_pct - cell.benchmark.total_return_pct
            self.assertAlmostEqual(cell.gap, expected)

    def test_the_report_states_how_many_settings_beat_the_benchmark(self):
        result = sweep_mod.run(self.series, "slow_trend",
                               {"period": [50, 100, 200]}, **self.kwargs)
        text = sweep_mod.render(result)
        self.assertIn("beat buy-and-hold", text)
        self.assertIn("settings tested", text)
        self.assertIn("spread", text)

    def test_a_wide_spread_triggers_the_cherry_picking_warning(self):
        """The whole reason this tool exists."""
        from tradebot.metrics import summarise
        from tradebot.types import EquityPoint

        def metrics(total):
            curve = [EquityPoint(ts=0, equity=1000.0, price=1.0, position=0.0, cash=1000.0),
                     EquityPoint(ts=10**10, equity=1000.0 * (1 + total / 100),
                                 price=1.0, position=0.0, cash=0.0)]
            return summarise(curve, [], starting_equity=1000.0, fees_paid=0.0)

        bench = metrics(0.0)
        s = sweep_mod.Sweep(strategy="test")
        for i, total in enumerate((-60.0, 2.0, 70.0)):
            s.cells.append(sweep_mod.Cell({"period": i}, "A", metrics(total), bench))
        text = sweep_mod.render(s)
        self.assertIn("reporting a choice, not a finding", text)

    def test_an_empty_sweep_does_not_crash(self):
        self.assertIn("Nothing to sweep", sweep_mod.render(sweep_mod.Sweep(strategy="x")))

    def test_drawdown_cut_is_reported_separately_from_return(self):
        result = sweep_mod.run(self.series, "slow_trend", {"period": [100]}, **self.kwargs)
        text = sweep_mod.render(result)
        self.assertIn("Drawdown was reduced in", text)
