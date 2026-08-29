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


class TestTrailingStop(unittest.TestCase):
    """A stop that follows price down is not a stop."""

    def _stops(self, closes):
        from tradebot.strategies import build
        from tradebot.strategies.base import Context

        strategy = build("ema_cross", fast=2, slow=3, atr_period=2, stop_atr_multiple=1.0)
        costs = CostModel()
        seen = []
        for i, close in enumerate(closes, start=1):
            ctx = Context(exposure=0.0 if i == 1 else 1.0, equity=1000.0,
                          costs=costs, avg_price=closes[0])
            decision = strategy.on_candle(
                candle(i, close, close * 1.01, close * 0.99, close), ctx
            )
            if decision.stop_loss is not None:
                seen.append(decision.stop_loss)
        return seen

    def test_the_stop_rises_with_the_price(self):
        stops = self._stops([100, 105, 110, 115, 120, 125])
        self.assertTrue(all(b >= a for a, b in zip(stops, stops[1:])), stops)
        self.assertGreater(stops[-1], stops[0])

    def test_the_stop_never_falls_when_the_price_does(self):
        rising = [100, 110, 120, 130, 140]
        stops = self._stops(rising + [135, 125, 115, 105, 95])
        self.assertTrue(
            all(b >= a - 1e-9 for a, b in zip(stops, stops[1:])),
            f"the stop retreated as price fell: {stops}",
        )

    def test_it_resets_once_the_position_is_closed(self):
        from tradebot.strategies import build
        from tradebot.strategies.base import Context

        strategy = build("ema_cross", fast=2, slow=3, atr_period=2, stop_atr_multiple=1.0)
        costs = CostModel()
        held = Context(exposure=1.0, equity=1000.0, costs=costs, avg_price=100.0)
        for i, close in enumerate([100, 200, 300, 400], start=1):
            strategy.on_candle(candle(i, close, close, close, close), held)
        high_stop = strategy._trail

        flat = Context(exposure=0.0, equity=1000.0, costs=costs)
        strategy.on_candle(candle(9, 50, 50, 50, 50), flat)
        self.assertLess(strategy._trail, high_stop, "a new position must not inherit the old stop")


class TestRejectionHandling(unittest.TestCase):
    """A rejected order that keeps being rejected must not be retried forever."""

    def _rejecting_engine(self, error_after=0, limit=3):
        from tradebot.brokers.base import Broker, BrokerError

        class Rejecting(Broker):
            is_live = True

            def __init__(self):
                self.calls = 0

            def execute(self, ts, signed_qty, reference_price, reason, liquidity=None):
                self.calls += 1
                raise BrokerError("insufficient funds")

        broker = Rejecting()
        costs = CostModel(taker_fee_bps=0, maker_fee_bps=0, half_spread_bps=0, slippage_bps=0)
        portfolio = Portfolio(starting_cash=1000.0)
        risk = RiskManager(
            limits=RiskLimits(max_position_pct=1.0, min_trade_notional=0.0,
                              max_trades_per_day=10_000, max_daily_loss_pct=0.99,
                              max_drawdown_pct=0.99),
            costs=costs,
        )
        engine = Engine(
            strategy=Scripted([Decision(1.0)] * 30), portfolio=portfolio, risk=risk,
            costs=costs, broker=broker,
            execution=ExecutionSettings(min_notional=0.0, max_consecutive_rejections=limit),
        )
        return engine, portfolio, risk, broker

    def test_repeated_rejections_halt_trading(self):
        engine, _, risk, broker = self._rejecting_engine(limit=3)
        for i in range(1, 12):
            engine.process(candle(i, 100, 100, 100, 100))

        self.assertEqual(risk.halted_reason, "repeated order rejections")
        self.assertLessEqual(broker.calls, 4, "must stop retrying, not hammer the venue")

    def test_a_dry_run_returning_none_is_not_a_rejection(self):
        """Dry-run withholds every order; that must never trip the rejection halt."""
        from tradebot.brokers.base import Broker

        class Withholding(Broker):
            is_live = True

            def execute(self, ts, signed_qty, reference_price, reason, liquidity=None):
                return None

        costs = CostModel(taker_fee_bps=0, maker_fee_bps=0, half_spread_bps=0, slippage_bps=0)
        portfolio = Portfolio(starting_cash=1000.0)
        risk = RiskManager(
            limits=RiskLimits(max_position_pct=1.0, min_trade_notional=0.0,
                              max_trades_per_day=10_000), costs=costs)
        engine = Engine(
            strategy=Scripted([Decision(1.0)] * 30), portfolio=portfolio, risk=risk,
            costs=costs, broker=Withholding(),
            execution=ExecutionSettings(min_notional=0.0, max_consecutive_rejections=3),
        )
        for i in range(1, 12):
            engine.process(candle(i, 100, 100, 100, 100))

        self.assertIsNone(risk.halted_reason)
        self.assertEqual(engine.consecutive_rejections, 0)

    def test_a_successful_fill_resets_the_count(self):
        from tradebot.brokers.base import Broker, BrokerError
        from tradebot.brokers.paper import PaperBroker

        costs = CostModel(taker_fee_bps=0, maker_fee_bps=0, half_spread_bps=0, slippage_bps=0)

        class Flaky(Broker):
            is_live = True

            def __init__(self):
                self.n = 0
                self.paper = PaperBroker(costs)

            def execute(self, ts, signed_qty, reference_price, reason, liquidity=None):
                self.n += 1
                if self.n <= 2:
                    raise BrokerError("temporary")
                return self.paper.execute(ts, signed_qty, reference_price, reason)

        portfolio = Portfolio(starting_cash=1000.0)
        risk = RiskManager(
            limits=RiskLimits(max_position_pct=1.0, min_trade_notional=0.0,
                              max_trades_per_day=10_000), costs=costs)
        engine = Engine(
            strategy=Scripted([Decision(1.0)] * 30), portfolio=portfolio, risk=risk,
            costs=costs, broker=Flaky(),
            execution=ExecutionSettings(min_notional=0.0, max_consecutive_rejections=5),
        )
        for i in range(1, 8):
            engine.process(candle(i, 100, 100, 100, 100))

        self.assertEqual(engine.consecutive_rejections, 0, "a fill clears the streak")
        self.assertIsNone(risk.halted_reason)
        self.assertFalse(portfolio.is_flat)

    def test_the_rejection_count_survives_a_restart(self):
        engine, _, _, _ = self._rejecting_engine(limit=10)
        for i in range(1, 4):
            engine.process(candle(i, 100, 100, 100, 100))
        saved = engine.state()
        self.assertGreater(saved["consecutive_rejections"], 0)

        fresh, _, _, _ = self._rejecting_engine(limit=10)
        fresh.restore(saved)
        self.assertEqual(fresh.consecutive_rejections, saved["consecutive_rejections"])


class TestCashSolvency(unittest.TestCase):
    """You cannot spend money you do not have, and a long cannot go below zero."""

    def _engine(self, cash=1000.0, costs=None):
        costs = costs or CostModel(taker_fee_bps=10, maker_fee_bps=10,
                                   half_spread_bps=2, slippage_bps=2)
        portfolio = Portfolio(starting_cash=cash)
        risk = RiskManager(
            limits=RiskLimits(max_position_pct=1.0, min_trade_notional=0.0,
                              max_trades_per_day=10_000, max_daily_loss_pct=0.99,
                              max_drawdown_pct=0.99),
            costs=costs,
        )
        engine = Engine(
            strategy=Scripted([Decision(1.0)] * 20), portfolio=portfolio, risk=risk,
            costs=costs, execution=ExecutionSettings(min_notional=0.0),
        )
        return engine, portfolio

    def test_a_full_size_buy_leaves_cash_at_zero_not_below(self):
        engine, portfolio = self._engine()
        engine.process(candle(1, 100, 100, 100, 100))
        engine.process(candle(2, 100, 100, 100, 100))
        self.assertGreaterEqual(portfolio.cash, -1e-6,
                                "sizing on equity then charging the fee is an overdraft")

    def test_measured_exposure_lands_on_its_target(self):
        """The drift that forced hold_weight and the rebalance threshold came from this."""
        engine, portfolio = self._engine()
        engine.process(candle(1, 100, 100, 100, 100))
        engine.process(candle(2, 100, 100, 100, 100))
        self.assertAlmostEqual(portfolio.exposure(100.0), 1.0, places=6)

    def test_a_long_cannot_end_with_negative_equity(self):
        """Unleveraged, the worst case is losing everything - not owing money."""
        engine, portfolio = self._engine()
        for i in range(1, 4):
            engine.process(candle(i, 100, 100, 100, 100))
        engine.process(candle(4, 0.01, 0.01, 0.01, 0.01))
        self.assertGreaterEqual(portfolio.equity(0.01), 0.0)

    def test_a_flat_fee_is_budgeted_for_too(self):
        costs = CostModel(taker_fee_bps=0, maker_fee_bps=0, half_spread_bps=0,
                          slippage_bps=0, flat_fee=25.0)
        engine, portfolio = self._engine(cash=1000.0, costs=costs)
        engine.process(candle(1, 100, 100, 100, 100))
        engine.process(candle(2, 100, 100, 100, 100))
        self.assertGreaterEqual(portfolio.cash, -1e-6)


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


class TestDailyOrderCap(unittest.TestCase):
    """The cap has to bind on what the engine actually sends, not on a counter."""

    class Ladder(Strategy):
        """Only ever adds: 10% of equity, then 20%, then 30%. Closes nothing, ever."""

        name = "ladder"

        def __init__(self):
            self.weight = 0.0

        def on_candle(self, c, ctx):
            self.weight = min(1.0, self.weight + 0.1)
            return Decision(self.weight, reason="add")

    def test_scaling_in_cannot_walk_past_the_daily_cap(self):
        """The evasion this closed: no round trip ever completes, so nothing was counted.

        Every one of those orders pays commission and takes an API call, which is what
        the cap is for. Counting round trips alone let a cap of three pass ten orders.
        """
        limits = RiskLimits(
            max_position_pct=1.0, max_daily_loss_pct=0.99, max_drawdown_pct=0.99,
            max_trades_per_day=3, min_trade_notional=1.0, cooldown_bars_after_loss=0,
        )
        engine, portfolio, _ = make_engine(self.Ladder(), cash=10_000.0, limits=limits)

        # Thirty bars inside a single UTC day, so the counter never resets.
        for i in range(30):
            engine.process(candle(i, 100.0, 100.5, 99.5, 100.0))

        self.assertLessEqual(
            len(portfolio.fills), 3,
            f"{len(portfolio.fills)} orders sent under a cap of 3",
        )

    def test_a_new_day_restores_the_budget(self):
        limits = RiskLimits(
            max_position_pct=1.0, max_daily_loss_pct=0.99, max_drawdown_pct=0.99,
            max_trades_per_day=2, min_trade_notional=1.0, cooldown_bars_after_loss=0,
        )
        engine, portfolio, _ = make_engine(self.Ladder(), cash=10_000.0, limits=limits)

        for i in range(10):
            engine.process(candle(i, 100.0, 100.5, 99.5, 100.0))
        first_day = len(portfolio.fills)

        # A day and a bit later, in minutes.
        for i in range(10):
            engine.process(candle(2000 + i, 100.0, 100.5, 99.5, 100.0))

        self.assertLessEqual(first_day, 2)
        self.assertGreater(len(portfolio.fills), first_day, "the cap must lift overnight")


if __name__ == "__main__":
    unittest.main()


class TestHoldingKeepsItsProtection(unittest.TestCase):
    """A stop set on entry must survive the bars where the strategy just holds.

    This was a live bug. "Hold, change nothing" cleared the brackets, so a strategy
    that set a stop on entry and then returned a bare Decision(None) lost that stop on
    the very next bar and carried the next crash uncapped. The stop was right there in
    the entry decision, gone before it could ever fire, and nothing reported it.

    None of the shipped strategies were bitten, because each happens to re-assert its
    brackets every bar. That is luck, not a design, and it is exactly the trap someone
    writing their own strategy would fall into.
    """

    def test_a_stop_survives_a_bare_hold(self):
        # explicit holds: the default fallback in Scripted is "go flat", not "hold"
        strategy = Scripted([Decision(1.0, stop_loss=95.0, reason="enter")]
                            + [Decision(None, reason="hold")] * 5)
        engine, portfolio, _ = make_engine(strategy, cash=10_000.0)

        engine.process(candle(1, 100, 100, 100, 100))
        engine.process(candle(2, 100, 101, 99, 100))
        self.assertAlmostEqual(engine.active_stop, 95.0)

        engine.process(candle(3, 100, 101, 99, 100))     # a bare hold
        self.assertAlmostEqual(engine.active_stop, 95.0, msg="the stop was silently dropped")

        engine.process(candle(4, 94, 94, 90, 91))        # crash
        self.assertTrue(portfolio.is_flat, "the stop did not fire")

    def test_a_named_bracket_still_replaces_the_old_one(self):
        """Silence is preserved; an instruction is obeyed - that is what lets stops trail."""
        strategy = Scripted([
            Decision(1.0, stop_loss=95.0, reason="enter"),
            Decision(None, stop_loss=98.0, reason="trail it up"),
            Decision(None, reason="hold"),
        ])
        engine, _, _ = make_engine(strategy, cash=10_000.0)
        engine.process(candle(1, 100, 100, 100, 100))
        engine.process(candle(2, 100, 101, 99, 100))
        engine.process(candle(3, 100, 101, 99, 100))
        self.assertAlmostEqual(engine.active_stop, 98.0)

    def test_going_flat_still_clears_everything(self):
        strategy = Scripted([Decision(1.0, stop_loss=95.0, reason="enter"),
                             Decision(0.0, reason="out"), Decision(None, reason="hold")])
        engine, portfolio, _ = make_engine(strategy, cash=10_000.0)
        engine.process(candle(1, 100, 100, 100, 100))
        engine.process(candle(2, 100, 101, 99, 100))
        engine.process(candle(3, 100, 101, 99, 100))
        self.assertTrue(portfolio.is_flat)
        self.assertIsNone(engine.active_stop)
