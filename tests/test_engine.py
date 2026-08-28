"""Engine tests, focused on the ways a backtester usually lies to you."""

from __future__ import annotations

import unittest

from tradebot.costs import CostModel
from tradebot.engine import Engine, ExecutionSettings
from tradebot.portfolio import Portfolio
from tradebot.risk import RiskLimits, RiskManager
from tradebot.strategies.base import Strategy
from tradebot.types import Candle, Decision

MINUTE = 60_000


def candle(ts, open_, high, low, close):
    return Candle(ts=ts * MINUTE, open=open_, high=high, low=low, close=close, volume=1.0)


class Scripted(Strategy):
    """Returns a canned decision per bar, so tests control the strategy exactly."""

    name = "scripted"

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.seen = []

    def on_candle(self, c, ctx):
        self.seen.append((c, ctx))
        return self.decisions.pop(0) if self.decisions else Decision(0.0)


def make_engine(strategy, cash=1000.0, costs=None, limits=None):
    costs = costs or CostModel(taker_fee_bps=0, maker_fee_bps=0, half_spread_bps=0, slippage_bps=0)
    limits = limits or RiskLimits(max_position_pct=1.0, min_trade_notional=0.0, max_trades_per_day=10_000)
    portfolio = Portfolio(starting_cash=cash)
    risk = RiskManager(limits=limits, costs=costs)
    engine = Engine(
        strategy=strategy,
        portfolio=portfolio,
        risk=risk,
        costs=costs,
        execution=ExecutionSettings(min_notional=0.0),
    )
    return engine, portfolio, risk


class TestNoLookAhead(unittest.TestCase):
    def test_decision_fills_at_the_next_bar_open_not_this_close(self):
        """The classic backtest bug: trading at the close that produced the signal."""
        strategy = Scripted([Decision(1.0, reason="go long")])
        engine, portfolio, _ = make_engine(strategy)

        engine.process(candle(1, 100, 100, 100, 100))
        self.assertTrue(portfolio.is_flat, "must not trade on the bar that decided")

        engine.process(candle(2, 200, 200, 200, 200))
        self.assertAlmostEqual(portfolio.qty, 5.0, msg="1000 equity / 200 open price")
        self.assertAlmostEqual(portfolio.avg_price, 200.0, msg="filled at the open, not the 100 close")

    def test_warmup_bars_never_produce_orders(self):
        class Warming(Scripted):
            @property
            def warmup(self):
                return 3

        strategy = Warming([Decision(1.0) for _ in range(6)])
        engine, portfolio, _ = make_engine(strategy)

        for i in range(1, 5):
            engine.process(candle(i, 100, 100, 100, 100))
        self.assertTrue(portfolio.is_flat, "no position until warmup is complete")

        engine.process(candle(5, 100, 100, 100, 100))
        self.assertFalse(portfolio.is_flat, "trades once warm")


class TestBrackets(unittest.TestCase):
    def test_stop_loss_exits_when_the_low_touches_it(self):
        strategy = Scripted([Decision(1.0, stop_loss=90.0), Decision(1.0, stop_loss=90.0)])
        engine, portfolio, _ = make_engine(strategy)

        engine.process(candle(1, 100, 100, 100, 100))
        engine.process(candle(2, 100, 100, 100, 100))
        self.assertFalse(portfolio.is_flat)

        engine.process(candle(3, 100, 101, 89, 95))
        self.assertTrue(portfolio.is_flat, "stop at 90 was inside the bar's range")
        self.assertAlmostEqual(portfolio.trades[-1].exit_price, 90.0)

    def test_take_profit_exits_when_the_high_touches_it(self):
        strategy = Scripted([Decision(1.0, take_profit=110.0), Decision(1.0, take_profit=110.0)])
        engine, portfolio, _ = make_engine(strategy)

        engine.process(candle(1, 100, 100, 100, 100))
        engine.process(candle(2, 100, 100, 100, 100))
        engine.process(candle(3, 100, 111, 99, 105))

        self.assertTrue(portfolio.is_flat)
        self.assertAlmostEqual(portfolio.trades[-1].exit_price, 110.0)

    def test_a_gap_below_the_stop_fills_at_the_open_not_the_stop(self):
        """A stop is a market order: gapping through it costs you the gap."""
        strategy = Scripted([Decision(1.0, stop_loss=90.0)] * 4)
        engine, portfolio, _ = make_engine(strategy)

        engine.process(candle(1, 100, 100, 100, 100))
        engine.process(candle(2, 100, 100, 100, 100))
        engine.process(candle(3, 80, 82, 79, 81))

        self.assertTrue(portfolio.is_flat)
        self.assertAlmostEqual(
            portfolio.trades[-1].exit_price, 80.0,
            msg="sold into the gap at the open, not at the untouchable 90 stop",
        )

    def test_a_gap_above_the_target_fills_at_the_open_not_the_target(self):
        """A take-profit is a limit order: gapping through it fills in your favour."""
        strategy = Scripted([Decision(1.0, take_profit=110.0)] * 4)
        engine, portfolio, _ = make_engine(strategy)

        engine.process(candle(1, 100, 100, 100, 100))
        engine.process(candle(2, 100, 100, 100, 100))
        engine.process(candle(3, 120, 122, 119, 121))

        self.assertTrue(portfolio.is_flat)
        self.assertAlmostEqual(
            portfolio.trades[-1].exit_price, 120.0,
            msg="filled at the better gap-open price, not capped at the 110 target",
        )

    def test_stop_wins_when_a_bar_touches_both(self):
        """A bar cannot say which came first, so the engine must assume the loss did."""
        strategy = Scripted([Decision(1.0, stop_loss=90.0, take_profit=110.0)] * 3)
        engine, portfolio, _ = make_engine(strategy)

        engine.process(candle(1, 100, 100, 100, 100))
        engine.process(candle(2, 100, 100, 100, 100))
        engine.process(candle(3, 100, 115, 85, 100))

        self.assertAlmostEqual(portfolio.trades[-1].exit_price, 90.0, msg="pessimistic reading")


class TestSizing(unittest.TestCase):
    def test_target_weight_sizes_against_equity(self):
        strategy = Scripted([Decision(0.5)])
        engine, portfolio, _ = make_engine(strategy, cash=1000.0)

        engine.process(candle(1, 100, 100, 100, 100))
        engine.process(candle(2, 100, 100, 100, 100))

        self.assertAlmostEqual(portfolio.qty, 5.0, msg="50% of 1000 at price 100")

    def test_qty_step_rounds_down_so_orders_are_never_oversized(self):
        strategy = Scripted([Decision(1.0)])
        costs = CostModel(taker_fee_bps=0, maker_fee_bps=0, half_spread_bps=0, slippage_bps=0)
        portfolio = Portfolio(starting_cash=1000.0)
        risk = RiskManager(limits=RiskLimits(max_position_pct=1.0, min_trade_notional=0.0), costs=costs)
        engine = Engine(
            strategy=strategy,
            portfolio=portfolio,
            risk=risk,
            costs=costs,
            execution=ExecutionSettings(qty_step=0.1, min_notional=0.0),
        )

        engine.process(candle(1, 100, 100, 100, 100))
        engine.process(candle(2, 300, 300, 300, 300))

        self.assertAlmostEqual(portfolio.qty, 3.3, places=9, msg="3.333 rounds down to 3.3")

    def test_orders_below_min_notional_are_skipped(self):
        strategy = Scripted([Decision(0.001)])
        costs = CostModel(taker_fee_bps=0, maker_fee_bps=0, half_spread_bps=0, slippage_bps=0)
        portfolio = Portfolio(starting_cash=1000.0)
        risk = RiskManager(limits=RiskLimits(max_position_pct=1.0, min_trade_notional=0.0), costs=costs)
        engine = Engine(
            strategy=strategy, portfolio=portfolio, risk=risk, costs=costs,
            execution=ExecutionSettings(min_notional=50.0),
        )

        engine.process(candle(1, 100, 100, 100, 100))
        engine.process(candle(2, 100, 100, 100, 100))
        self.assertTrue(portfolio.is_flat, "a £1 order should not be sent when the minimum is £50")


class TestRebalanceThreshold(unittest.TestCase):
    def test_small_drift_does_not_trigger_a_trade(self):
        """Staying 'fully invested' must not mean trading every single bar."""
        strategy = Scripted([Decision(1.0)] * 10)
        costs = CostModel(taker_fee_bps=10, maker_fee_bps=10, half_spread_bps=2, slippage_bps=2)
        portfolio = Portfolio(starting_cash=10_000.0)
        risk = RiskManager(
            limits=RiskLimits(max_position_pct=1.0, min_trade_notional=0.0, max_trades_per_day=10_000),
            costs=costs,
        )
        engine = Engine(
            strategy=strategy, portfolio=portfolio, risk=risk, costs=costs,
            execution=ExecutionSettings(min_notional=0.0, rebalance_threshold=0.005),
        )

        for i in range(1, 9):
            engine.process(candle(i, 100, 100, 100, 100))

        self.assertEqual(len(portfolio.fills), 1, "one entry, then nothing to correct")

    def test_the_threshold_never_blocks_closing_a_position(self):
        strategy = Scripted([Decision(1.0), Decision(1.0), Decision(0.0), Decision(0.0)])
        costs = CostModel(taker_fee_bps=0, maker_fee_bps=0, half_spread_bps=0, slippage_bps=0)
        portfolio = Portfolio(starting_cash=10_000.0)
        risk = RiskManager(
            limits=RiskLimits(max_position_pct=1.0, min_trade_notional=0.0), costs=costs
        )
        engine = Engine(
            strategy=strategy, portfolio=portfolio, risk=risk, costs=costs,
            execution=ExecutionSettings(min_notional=0.0, rebalance_threshold=0.9),
        )

        for i in range(1, 5):
            engine.process(candle(i, 100, 100, 100, 100))

        self.assertTrue(portfolio.is_flat, "an exit is not a resize and must always go through")

    def test_a_flat_fee_does_not_start_a_runaway_rebalance_loop(self):
        """Regression: the fee creates the drift, so correcting it drifts further.

        With a flat commission large relative to the position, an entry fee pushes
        measured exposure past the rebalance threshold. Trading to correct that pays
        another flat fee and widens the gap again. Left unguarded this turns a
        buy-and-hold into hundreds of trades and empties the account on costs.
        """
        strategy = Scripted([Decision(1.0)] * 40)
        costs = CostModel(taker_fee_bps=0, maker_fee_bps=0, half_spread_bps=1,
                          slippage_bps=2, flat_fee=6.0)
        portfolio = Portfolio(starting_cash=1000.0)
        risk = RiskManager(
            limits=RiskLimits(max_position_pct=1.0, min_trade_notional=0.0,
                              max_trades_per_day=10_000),
            costs=costs,
        )
        engine = Engine(
            strategy=strategy, portfolio=portfolio, risk=risk, costs=costs,
            execution=ExecutionSettings(min_notional=0.0, rebalance_threshold=0.005),
        )

        for i in range(1, 35):
            engine.process(candle(i, 100, 100, 100, 100))

        self.assertEqual(len(portfolio.fills), 1, "must buy once and then stop trading")
        self.assertLess(portfolio.fees_paid, 10.0, "one flat fee, not dozens")

    def test_a_large_deliberate_resize_still_trades(self):
        strategy = Scripted([Decision(1.0), Decision(1.0), Decision(0.4), Decision(0.4)])
        engine, portfolio, _ = make_engine(strategy, cash=10_000.0)

        for i in range(1, 5):
            engine.process(candle(i, 100, 100, 100, 100))

        self.assertAlmostEqual(portfolio.exposure(100.0), 0.4, places=2)


class TestHaltBehaviour(unittest.TestCase):
    def test_a_halt_closes_an_open_position_rather_than_freezing_it(self):
        """Stopping trading must also mean stopping exposure."""
        strategy = Scripted([Decision(1.0)] * 6)
        limits = RiskLimits(
            max_position_pct=1.0, min_trade_notional=0.0,
            max_daily_loss_pct=0.02, max_drawdown_pct=0.99, max_trades_per_day=1000,
        )
        engine, portfolio, risk = make_engine(strategy, limits=limits)

        engine.process(candle(1, 100, 100, 100, 100))
        engine.process(candle(2, 100, 100, 100, 100))
        self.assertFalse(portfolio.is_flat, "should be long before the halt")

        # A 5% drop breaches the 2% daily loss limit.
        engine.process(candle(3, 95, 95, 95, 95))
        engine.process(candle(4, 95, 95, 95, 95))

        self.assertIsNotNone(risk.halted_reason)
        self.assertTrue(portfolio.is_flat, "the halt should have flattened the position")

    def test_no_new_position_is_opened_while_halted(self):
        strategy = Scripted([Decision(0.0), Decision(1.0), Decision(1.0), Decision(1.0)])
        engine, portfolio, risk = make_engine(strategy)
        risk.halted_reason = "max drawdown"

        for i in range(1, 5):
            engine.process(candle(i, 100, 100, 100, 100))

        self.assertTrue(portfolio.is_flat)


class TestCostsAreCharged(unittest.TestCase):
    def test_a_flat_market_still_loses_money_to_costs(self):
        """Buy and sell at the same price. You must end up poorer, not level."""
        strategy = Scripted([Decision(1.0), Decision(1.0), Decision(0.0)])
        costs = CostModel(taker_fee_bps=10, maker_fee_bps=5, half_spread_bps=2, slippage_bps=2)
        engine, portfolio, _ = make_engine(strategy, costs=costs)

        for i in range(1, 5):
            engine.process(candle(i, 100, 100, 100, 100))

        self.assertLess(portfolio.equity(100.0), 1000.0)
        self.assertGreater(portfolio.fees_paid, 0.0)
        self.assertGreater(portfolio.slippage_paid, 0.0)


if __name__ == "__main__":
    unittest.main()
