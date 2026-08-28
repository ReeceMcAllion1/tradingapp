"""End-to-end tests: strategies, the backtester, and the live runner's plumbing."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tradebot import backtest
from tradebot.brokers.cryptocom import CryptoComBroker, params_to_str
from tradebot.brokers.paper import PaperBroker
from tradebot.config import Config
from tradebot.costs import CostModel
from tradebot.feeds.base import validate_series
from tradebot.feeds.synthetic import SyntheticFeed
from tradebot.live import LiveRunner
from tradebot.risk import RiskLimits
from tradebot.strategies import available, build
from tradebot.types import Candle


class StubFeed:
    """Replays a fixed series, so the live runner can be tested without a network."""

    def __init__(self, candles):
        self.candles = candles

    def history(self, limit):
        return self.candles[:limit]

    def stream(self):
        yield from self.candles


class RealisticFeed:
    """Behaves like a live feed: recent history, and a stream that re-polls.

    ``StubFeed`` returns the *first* bars from history and then streams the whole
    series, which never overlaps. Real feeds return the most recent bars and then poll
    for the newest closed one - which, on a fresh stream, is the bar warm-up just
    finished on. That overlap is the thing being tested.
    """

    def __init__(self, candles, upto):
        self.candles = candles
        self.upto = upto

    def history(self, limit):
        return self.candles[: self.upto][-limit:]

    def stream(self):
        yield from self.candles[self.upto - 1 :]


class TestStrategies(unittest.TestCase):
    def setUp(self):
        self.candles = SyntheticFeed(bars=1500, seed=5).generate()

    def test_every_registered_strategy_runs_end_to_end(self):
        for name in available():
            with self.subTest(strategy=name):
                result = backtest.run(self.candles, build(name), starting_cash=1000.0)
                self.assertGreater(result.metrics.bars, 0)
                self.assertIsInstance(result.metrics.net_pnl, float)

    def test_a_strategy_never_exceeds_the_position_cap(self):
        limits = RiskLimits(max_position_pct=0.25, max_trades_per_day=10_000)
        result = backtest.run(self.candles, build("ema_cross"), limits=limits)
        for point in result.engine.portfolio.equity_curve:
            exposure = abs(point.position * point.price) / point.equity if point.equity else 0.0
            self.assertLessEqual(exposure, 0.30, msg="25% cap, with headroom for drift within a bar")

    def test_micro_scalp_loses_to_costs_but_predicts_direction_well(self):
        """The central claim of this package, checked on generated data."""
        result = backtest.run(self.candles, build("micro_scalp"), starting_cash=1000.0)
        metrics = result.metrics

        self.assertGreater(metrics.trades, 20, "should trade often")
        self.assertGreater(metrics.gross_win_rate, 0.7, "most trades call direction right")
        self.assertLess(metrics.net_pnl, 0.0, "and it still loses money")
        self.assertGreater(metrics.total_costs, abs(metrics.gross_pnl), "costs exceed the edge")

    def test_a_zero_cost_world_changes_the_verdict(self):
        """Proof the loss is caused by costs, not by the entry rule being nonsense."""
        free = CostModel(taker_fee_bps=0, maker_fee_bps=0, half_spread_bps=0, slippage_bps=0)
        charged = CostModel()

        free_result = backtest.run(self.candles, build("micro_scalp"), costs=free)
        paid_result = backtest.run(self.candles, build("micro_scalp"), costs=charged)

        self.assertGreater(
            free_result.metrics.net_pnl, paid_result.metrics.net_pnl,
            msg="removing costs must improve the same strategy on the same data",
        )

    def test_mean_reversion_trades_less_than_the_scalper(self):
        scalp = backtest.run(self.candles, build("micro_scalp"))
        careful = backtest.run(self.candles, build("mean_reversion"))
        self.assertLess(
            careful.metrics.trades, scalp.metrics.trades,
            msg="the cost filter should suppress most signals",
        )


class TestCostWarnings(unittest.TestCase):
    """A strategy must object to its own impossible parameters before it runs."""

    def test_a_target_below_the_round_trip_produces_a_warning(self):
        strategy = build("micro_scalp", profit_target_pct=0.0005)
        warnings = strategy.cost_warnings(CostModel())
        self.assertEqual(len(warnings), 1)
        self.assertIn("0.280%", warnings[0])

    def test_a_target_above_the_round_trip_is_silent(self):
        strategy = build("micro_scalp", profit_target_pct=0.01)
        self.assertEqual(strategy.cost_warnings(CostModel()), [])

    def test_a_zero_cost_venue_silences_the_warning(self):
        free = CostModel(taker_fee_bps=0, maker_fee_bps=0, half_spread_bps=0, slippage_bps=0)
        self.assertEqual(build("micro_scalp").cost_warnings(free), [])

    def test_strategies_without_a_fixed_target_stay_quiet(self):
        for name in ("ema_cross", "mean_reversion"):
            self.assertEqual(build(name).cost_warnings(CostModel()), [])


class TestBacktester(unittest.TestCase):
    def test_an_empty_series_is_rejected(self):
        with self.assertRaises(ValueError):
            backtest.run([], build("ema_cross"))

    def test_the_run_ends_flat(self):
        candles = SyntheticFeed(bars=600, seed=2).generate()
        result = backtest.run(candles, build("ema_cross"))
        self.assertTrue(result.engine.portfolio.is_flat, "must close out at the end")

    def test_net_pnl_matches_the_change_in_equity(self):
        candles = SyntheticFeed(bars=800, seed=9).generate()
        result = backtest.run(candles, build("ema_cross"), starting_cash=1000.0)
        self.assertAlmostEqual(
            result.metrics.net_pnl,
            result.metrics.ending_equity - result.metrics.starting_equity,
            places=6,
        )

    def test_gross_minus_costs_equals_net(self):
        """The three reported figures must actually reconcile."""
        candles = SyntheticFeed(bars=800, seed=4).generate()
        result = backtest.run(candles, build("micro_scalp"), starting_cash=1000.0)
        metrics = result.metrics
        self.assertAlmostEqual(
            metrics.gross_pnl - metrics.slippage_paid - metrics.fees_paid,
            metrics.net_pnl,
            places=4,
        )

    def test_higher_costs_never_produce_a_better_result(self):
        candles = SyntheticFeed(bars=1000, seed=6).generate()
        cheap = backtest.run(candles, build("ema_cross"), costs=CostModel(taker_fee_bps=1))
        dear = backtest.run(candles, build("ema_cross"), costs=CostModel(taker_fee_bps=50))
        self.assertGreaterEqual(cheap.metrics.net_pnl, dear.metrics.net_pnl)


class TestFeeds(unittest.TestCase):
    def test_duplicate_timestamps_are_collapsed(self):
        rows = [
            Candle(ts=100, open=1, high=1, low=1, close=1, volume=1),
            Candle(ts=100, open=2, high=2, low=2, close=2, volume=1),
            Candle(ts=200, open=3, high=3, low=3, close=3, volume=1),
        ]
        cleaned = validate_series(rows)
        self.assertEqual(len(cleaned), 2)
        self.assertAlmostEqual(cleaned[0].close, 2.0, msg="the later duplicate wins")

    def test_an_empty_series_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_series([])

    def test_synthetic_data_is_deterministic(self):
        first = SyntheticFeed(bars=50, seed=42).generate()
        second = SyntheticFeed(bars=50, seed=42).generate()
        self.assertEqual([c.close for c in first], [c.close for c in second])


class TestLiveRunner(unittest.TestCase):
    def _runner(self, tmpdir, candles):
        config = Config()
        config.live.state_file = str(Path(tmpdir) / "state.json")
        # Must be redirected too. Left at its default this writes fabricated trades
        # into the real state/trades.csv, which is the file preflight reads to decide
        # whether enough paper trading has happened to risk real money.
        config.live.trades_file = str(Path(tmpdir) / "trades.csv")
        config.account.starting_cash = 1000.0
        config.risk = RiskLimits(max_position_pct=0.5, max_trades_per_day=1000)
        return LiveRunner(
            config=config,
            strategy=build("ema_cross", fast=3, slow=8),
            feed=StubFeed(candles),
            broker=PaperBroker(config.costs),
        )

    def test_the_runner_never_writes_to_the_real_state_directory(self):
        """Regression: a test run must not fabricate live paper-trading history."""
        with tempfile.TemporaryDirectory() as tmp:
            runner = self._runner(tmp, SyntheticFeed(bars=50, seed=1).generate())
            for path in (runner.config.live.state_file, runner.config.live.trades_file):
                self.assertTrue(
                    path.startswith(tmp),
                    f"{path} escapes the temp directory and would pollute the real record",
                )

    def test_a_paper_session_runs_and_saves_state(self):
        candles = SyntheticFeed(bars=300, seed=1).generate()
        with tempfile.TemporaryDirectory() as tmp:
            runner = self._runner(tmp, candles)
            with contextlib.redirect_stdout(io.StringIO()):
                runner.run(max_bars=200)

            state_file = Path(runner.config.live.state_file)
            self.assertTrue(state_file.exists())
            saved = json.loads(state_file.read_text())
            self.assertEqual(saved["symbol"], "BTC_USD")
            self.assertIn("portfolio", saved["engine"])

    def test_state_survives_a_restart(self):
        candles = SyntheticFeed(bars=300, seed=1).generate()
        with tempfile.TemporaryDirectory() as tmp:
            first = self._runner(tmp, candles)
            with contextlib.redirect_stdout(io.StringIO()):
                first.run(max_bars=150)
            cash_before = first.portfolio.cash
            qty_before = first.portfolio.qty

            second = self._runner(tmp, candles)
            self.assertTrue(second.load_state())
            self.assertAlmostEqual(second.portfolio.cash, cash_before)
            self.assertAlmostEqual(second.portfolio.qty, qty_before)

    def test_state_for_a_different_symbol_is_ignored(self):
        candles = SyntheticFeed(bars=100, seed=1).generate()
        with tempfile.TemporaryDirectory() as tmp:
            runner = self._runner(tmp, candles)
            with contextlib.redirect_stdout(io.StringIO()):
                runner.run(max_bars=50)

            other = self._runner(tmp, candles)
            other.config.market.symbol = "ETH_USD"
            self.assertFalse(other.load_state(), "must not resume another market's position")

    def _counting_runner(self, tmpdir, feed):
        from tradebot.strategies.base import Strategy
        from tradebot.types import Decision

        class Counting(Strategy):
            name = "counting"
            warmup = 0

            def __init__(self):
                self.seen = []

            def on_candle(self, c, ctx):
                self.seen.append(c.ts)
                return Decision(0.0, reason="flat")

        config = Config()
        config.live.state_file = str(Path(tmpdir) / "state.json")
        config.live.trades_file = str(Path(tmpdir) / "trades.csv")
        config.account.starting_cash = 1000.0
        config.risk = RiskLimits(max_position_pct=0.5, max_trades_per_day=1000)
        strategy = Counting()
        return LiveRunner(config=config, strategy=strategy, feed=feed,
                          broker=PaperBroker(config.costs)), strategy

    def test_a_bar_is_never_shown_to_the_strategy_twice(self):
        """Warm-up ends on the newest closed bar; the stream then offers it again.

        Replaying it advances every indicator an extra step on a bar that only
        happened once, shifting every signal after it. This is not a crash-only case -
        it happened on every single start.
        """
        candles = SyntheticFeed(bars=200, seed=1).generate()
        with tempfile.TemporaryDirectory() as tmp:
            runner, strategy = self._counting_runner(tmp, RealisticFeed(candles, 100))
            with contextlib.redirect_stdout(io.StringIO()):
                runner.run(max_bars=4)

            self.assertEqual(
                len(strategy.seen), len(set(strategy.seen)),
                "the same bar reached the strategy more than once",
            )

    def test_a_restart_does_not_reprocess_the_bar_it_stopped_on(self):
        candles = SyntheticFeed(bars=200, seed=1).generate()
        with tempfile.TemporaryDirectory() as tmp:
            first, _ = self._counting_runner(tmp, RealisticFeed(candles, 100))
            with contextlib.redirect_stdout(io.StringIO()):
                first.run(max_bars=3)
            stopped_at = first._last_bar_ts

            second, strategy = self._counting_runner(tmp, RealisticFeed(candles, 100))
            with contextlib.redirect_stdout(io.StringIO()):
                second.run(max_bars=3)

            engine_bars = [
                point.ts for point in second.portfolio.equity_curve
            ]
            self.assertTrue(
                all(ts > stopped_at for ts in engine_bars),
                f"the resumed run re-processed bars at or before {stopped_at}",
            )

    def test_an_out_of_order_bar_is_ignored(self):
        """An older bar would re-open a risk day that has already closed."""
        candles = SyntheticFeed(bars=200, seed=1).generate()
        with tempfile.TemporaryDirectory() as tmp:
            runner, strategy = self._counting_runner(tmp, RealisticFeed(candles, 100))
            runner.warm_up()
            before = len(runner.portfolio.equity_curve)

            runner.on_bar(candles[10])
            self.assertEqual(len(runner.portfolio.equity_curve), before,
                             "a bar from the past was processed")

            runner.on_bar(candles[150])
            self.assertEqual(len(runner.portfolio.equity_curve), before + 1)

    def test_a_corrupt_state_file_starts_fresh_rather_than_crashing(self):
        candles = SyntheticFeed(bars=100, seed=1).generate()
        with tempfile.TemporaryDirectory() as tmp:
            runner = self._runner(tmp, candles)
            Path(runner.config.live.state_file).write_text("{not json", encoding="utf-8")
            self.assertFalse(runner.load_state())


class TestLiveBrokerSafety(unittest.TestCase):
    """The gates that stand between a bug and your actual money."""

    def _broker(self, **kwargs):
        return CryptoComBroker(symbol="BTC_USD", costs=CostModel(), **kwargs)

    def test_orders_are_not_sent_while_disabled(self):
        broker = self._broker(enabled=False, dry_run=False)
        self.assertIsNone(broker.execute(1, 0.001, 30_000.0, "test"))

    def test_orders_are_not_sent_during_a_dry_run(self):
        broker = self._broker(enabled=True, dry_run=True)
        self.assertIsNone(broker.execute(1, 0.001, 30_000.0, "test"))

    def test_defaults_are_the_safe_ones(self):
        broker = self._broker()
        self.assertFalse(broker.enabled)
        self.assertTrue(broker.dry_run)

    def test_missing_credentials_raise_rather_than_silently_no_op(self):
        import os
        from tradebot.brokers.base import BrokerError

        saved = {k: os.environ.pop(k, None) for k in ("CRYPTOCOM_API_KEY", "CRYPTOCOM_API_SECRET")}
        try:
            with self.assertRaises(BrokerError):
                self._broker().verify()
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    def test_signing_payload_is_canonical_and_stable(self):
        self.assertEqual(params_to_str({"b": 2, "a": 1}), "a1b2")
        self.assertEqual(params_to_str({"flag": True}), "flagtrue")
        self.assertEqual(params_to_str({"empty": None}), "emptynull")
        self.assertEqual(params_to_str({}), "")

    def test_signing_is_order_independent(self):
        self.assertEqual(
            params_to_str({"instrument_name": "BTC_USD", "side": "BUY"}),
            params_to_str({"side": "BUY", "instrument_name": "BTC_USD"}),
        )


if __name__ == "__main__":
    unittest.main()
