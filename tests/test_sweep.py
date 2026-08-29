"""Tests for the parameter sweep - the tool whose job is to expose cherry-picking."""

from __future__ import annotations

import unittest
from dataclasses import replace

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

    def test_the_mean_is_a_mean_and_not_the_best_cell(self):
        """The failure that would defeat the module's entire purpose.

        This tool exists to stop a single lucky parameter cell being presented as the
        result. If its "mean" quietly reported the maximum instead, the sweep would
        cherry-pick automatically while looking like the thing that prevents it - and
        it survived a mutation sweep, so nothing was checking.
        """
        from tradebot.metrics import Metrics
        from tradebot.sweep import Cell, Sweep

        def cell(params, ret, bench=0.0, dd=10.0, bench_dd=10.0):
            def metrics(total, drawdown):
                return Metrics(
                    starting_equity=1000.0, ending_equity=1000.0, total_return_pct=total,
                    cagr_pct=0.0, years=1.0, gross_pnl=0.0, net_pnl=0.0, fees_paid=0.0,
                    slippage_paid=0.0, max_drawdown_pct=drawdown, sharpe=0.0, trades=0,
                    win_rate=0.0, gross_win_rate=0.0, profit_factor=0.0, avg_trade=0.0,
                    best_trade=0.0, worst_trade=0.0, bars=1000,
                )
            return Cell(symbol="A", params=params, metrics=metrics(ret, dd),
                        benchmark=metrics(bench, bench_dd))

        params = {"period": 100}
        # One brilliant cell and three poor ones: mean -5, max +30.
        sweep = Sweep(strategy="x", cells=[
            cell(params, 30.0), cell(params, -10.0), cell(params, -20.0), cell(params, -20.0),
        ])
        self.assertAlmostEqual(sweep.mean_gap(params), -5.0)
        self.assertNotAlmostEqual(sweep.mean_gap(params), 30.0)

    def test_the_mean_only_averages_cells_with_those_settings(self):
        from tradebot.metrics import Metrics
        from tradebot.sweep import Cell, Sweep

        def cell(params, ret):
            m = Metrics(
                starting_equity=1000.0, ending_equity=1000.0, total_return_pct=ret,
                cagr_pct=0.0, years=1.0, gross_pnl=0.0, net_pnl=0.0, fees_paid=0.0,
                slippage_paid=0.0, max_drawdown_pct=0.0, sharpe=0.0, trades=0,
                win_rate=0.0, gross_win_rate=0.0, profit_factor=0.0, avg_trade=0.0,
                best_trade=0.0, worst_trade=0.0, bars=1000,
            )
            zero = replace(m, total_return_pct=0.0)
            return Cell(symbol="A", params=params, metrics=m, benchmark=zero)

        a, b = {"period": 50}, {"period": 200}
        sweep = Sweep(strategy="x", cells=[cell(a, 10.0), cell(a, 20.0), cell(b, -100.0)])
        self.assertAlmostEqual(sweep.mean_gap(a), 15.0)
        self.assertAlmostEqual(sweep.mean_gap(b), -100.0)

    def test_the_drawdown_cut_is_really_measured(self):
        result = sweep_mod.run(self.series, "slow_trend", {"period": [50, 200]}, **self.kwargs)
        cuts = [c.drawdown_cut for c in result.cells]
        self.assertTrue(cuts)
        self.assertTrue(any(abs(c) > 1e-9 for c in cuts),
                        "every cell reported an identical zero drawdown change")
        for c in result.cells:
            self.assertAlmostEqual(
                c.drawdown_cut,
                c.benchmark.max_drawdown_pct - c.metrics.max_drawdown_pct, places=9,
            )

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
