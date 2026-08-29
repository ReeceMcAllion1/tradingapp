"""Tests for walk-forward validation - the check on every other result here."""

from __future__ import annotations

import unittest

from tradebot import walkforward as wf
from tradebot.costs import CostModel
from tradebot.engine import ExecutionSettings
from tradebot.feeds.synthetic import SyntheticFeed
from tradebot.risk import RiskLimits


class TestWalkForward(unittest.TestCase):
    def setUp(self):
        self.series = {"A": SyntheticFeed(bars=4200, seed=3, drift_per_bar=0.0002).generate()}
        self.kwargs = dict(
            starting_cash=1000.0,
            costs=CostModel(taker_fee_bps=7.5, maker_fee_bps=7.5, half_spread_bps=1, slippage_bps=2),
            limits=RiskLimits(max_position_pct=1.0, max_daily_loss_pct=0.99,
                              max_drawdown_pct=0.99, max_trades_per_day=10_000,
                              min_trade_notional=1.0, cooldown_bars_after_loss=0),
            execution=ExecutionSettings(min_notional=1.0),
        )

    def test_it_produces_one_fold_per_split(self):
        result = wf.run(self.series, "slow_trend", {"period": [50, 150]}, folds=4, **self.kwargs)
        self.assertEqual(len(result.folds), 4)
        self.assertEqual([f.index for f in result.folds], [1, 2, 3, 4])

    def test_segments_are_consecutive_and_disjoint(self):
        candles = self.series["A"]
        chunks = wf._segments(candles, 5)
        self.assertEqual(len(chunks), 5)
        for a, b in zip(chunks, chunks[1:]):
            self.assertLess(a[-1].ts, b[0].ts, "a later segment must start after the earlier ends")

    def test_the_test_segment_is_never_the_training_segment(self):
        """The one bug that would make this whole module a liar.

        Walk-forward exists to answer a single question: does an edge chosen on past
        data survive on data the choice never saw? Point the test run at the training
        segment and it still produces folds, numbers and a confident verdict - all of
        it in-sample, all of it flattering, and every honest claim in this repository
        rests on it. Checking that ``_segments`` returns disjoint chunks does not cover
        this: the segments were always fine, it is which of them ``run`` hands to each
        backtest that matters.

        So this watches the actual calls and asserts no candle used for training is
        used again for testing.
        """
        from unittest import mock

        calls = []
        real = wf.backtest_mod.run

        def spy(**kwargs):
            calls.append(kwargs["candles"])
            return real(**kwargs)

        with mock.patch.object(wf.backtest_mod, "run", spy):
            result = wf.run(self.series, "slow_trend", {"period": [50, 150]}, folds=3, **self.kwargs)

        self.assertTrue(result.folds)
        spans = {(c[0].ts, c[-1].ts) for c in calls}
        self.assertGreater(len(spans), 1, "every backtest ran on the same bars")

        # Each fold trains on one segment and tests on the next, so a training span
        # must never reappear as a testing span within the same fold.
        chunks = wf._segments(self.series["A"], 4)
        for i in range(3):
            train_span = (chunks[i][0].ts, chunks[i][-1].ts)
            test_span = (chunks[i + 1][0].ts, chunks[i + 1][-1].ts)
            self.assertNotEqual(train_span, test_span)
            self.assertIn(train_span, spans)
            self.assertIn(test_span, spans)
            self.assertLess(train_span[1], test_span[0],
                            "the test segment must come strictly after the training one")

    def test_the_parameters_carried_forward_are_the_best_in_sample_ones(self):
        """Selecting the worst would understate the edge - wrong in the honest
        direction, but wrong. The whole premise is that this is the choice a hopeful
        person would have made, measured on what happened next."""
        grid = {"period": [50, 150, 300]}
        result = wf.run(self.series, "slow_trend", grid, folds=3, **self.kwargs)

        chunks = wf._segments(self.series["A"], 4)
        from tradebot import backtest as backtest_mod
        from tradebot.strategies import build

        for fold in result.folds:
            train = chunks[fold.index - 1]
            bench = backtest_mod.run(
                candles=train, strategy=build("buy_and_hold"), **self.kwargs
            ).metrics.total_return_pct
            gaps = {
                p: backtest_mod.run(
                    candles=train, strategy=build("slow_trend", period=p), **self.kwargs
                ).metrics.total_return_pct - bench
                for p in grid["period"]
            }
            self.assertEqual(
                fold.chosen["period"], max(gaps, key=gaps.get),
                f"fold {fold.index} did not carry forward the best in-sample choice",
            )
            self.assertAlmostEqual(fold.in_sample_gap, max(gaps.values()), places=6)

    def test_the_drawdown_cut_is_really_measured(self):
        """The one effect that survived out of sample, so it must not be a constant."""
        result = wf.run(self.series, "slow_trend", {"period": [50, 150]}, folds=3, **self.kwargs)
        cuts = [f.out_of_sample_drawdown_cut for f in result.folds]
        self.assertTrue(result.folds)
        self.assertTrue(any(abs(c) > 1e-9 for c in cuts),
                        "every fold reported an identical zero drawdown change")

    def test_the_chosen_parameters_come_from_the_grid(self):
        grid = {"period": [50, 150, 300]}
        result = wf.run(self.series, "slow_trend", grid, folds=3, **self.kwargs)
        for fold in result.folds:
            self.assertIn(fold.chosen["period"], grid["period"])

    def test_out_of_sample_is_measured_against_the_same_test_segment(self):
        result = wf.run(self.series, "slow_trend", {"period": [50, 150]}, folds=3, **self.kwargs)
        for fold in result.folds:
            self.assertAlmostEqual(
                fold.out_of_sample_gap,
                fold.out_of_sample_return - fold.benchmark_return,
                places=6,
            )

    def test_a_single_parameter_still_walks_forward(self):
        """With no choice to make, in-sample and out-of-sample still differ by regime."""
        result = wf.run(self.series, "slow_trend", {"period": [100]}, folds=3, **self.kwargs)
        self.assertEqual(len(result.folds), 3)

    def test_too_little_data_yields_no_folds_rather_than_crashing(self):
        tiny = {"A": SyntheticFeed(bars=120, seed=1).generate()}
        result = wf.run(tiny, "slow_trend", {"period": [50]}, folds=6, **self.kwargs)
        self.assertIn("Not enough data", wf.render(result))

    def test_the_report_separates_promised_from_delivered(self):
        result = wf.run(self.series, "slow_trend", {"period": [50, 150]}, folds=3, **self.kwargs)
        text = wf.render(result)
        self.assertIn("in-sample, as picked", text)
        self.assertIn("out-of-sample, actual", text)
        self.assertIn("lost to overfitting", text)

    def test_a_worthless_selection_is_called_out(self):
        result = wf.WalkForward(strategy="test")
        for i in range(4):
            result.folds.append(wf.Fold(
                symbol="A", index=i + 1, chosen={"period": 100},
                in_sample_gap=40.0, out_of_sample_gap=-5.0,
                out_of_sample_return=-5.0, benchmark_return=0.0,
                out_of_sample_drawdown_cut=1.0,
            ))
        self.assertIn("carry no", wf.render(result))

    def test_surviving_but_shrunken_edge_is_flagged_as_such(self):
        result = wf.WalkForward(strategy="test")
        for i in range(4):
            result.folds.append(wf.Fold(
                symbol="A", index=i + 1, chosen={"period": 100},
                in_sample_gap=40.0, out_of_sample_gap=5.0,
                out_of_sample_return=5.0, benchmark_return=0.0,
                out_of_sample_drawdown_cut=3.0,
            ))
        text = wf.render(result)
        self.assertIn("did not survive", text)
        self.assertIn("out-of-sample column and nothing else", text)


if __name__ == "__main__":
    unittest.main()
