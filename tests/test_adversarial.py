"""Hostile inputs, run against every registered strategy.

The unit tests check behaviour that was designed; this file checks behaviour that
was not. Each case here is a situation a market can produce and a strategy has no
say over - a series one bar long, a price that falls 99% overnight, an account too
small to place a single order - and each asserts an invariant that must hold no
matter which strategy is loaded.

Two of these caught real bugs. Sizing a long from equity and then charging the fee
spent money the account did not have, which showed up as cash a hair below zero and,
on a bad enough series, equity below zero on a long position - impossible without
leverage, and the hidden cause of two earlier "drift" patches. A flat commission
turned buy-and-hold into hundreds of trades, because paying the fee created the very
drift the next trade was correcting.

The invariants are written to be strategy-agnostic on purpose. A new strategy is
picked up here automatically, and if it can be made to break one of these it is the
strategy that is wrong.
"""

from __future__ import annotations

import unittest

from tradebot import backtest
from tradebot.costs import CostModel
from tradebot.engine import ExecutionSettings
from tradebot.feeds.synthetic import SyntheticFeed
from tradebot.risk import RiskLimits
from tradebot.strategies import available, build
from tradebot.strategies.base import Strategy
from tradebot.types import Candle, Decision, WEIGHT_TOLERANCE

MINUTE = 60_000

#: Deliberately permissive: the risk manager would otherwise halt these runs early
#: and the engine would never reach the states being tested.
WIDE_OPEN = dict(
    max_position_pct=1.0,
    max_daily_loss_pct=0.99,
    max_drawdown_pct=0.99,
    max_trades_per_day=100_000,
    min_trade_notional=1.0,
    cooldown_bars_after_loss=0,
)


def series(prices, ts_step=MINUTE, start_ts=1_700_000_000_000):
    """Bars with a real intrabar range, so brackets have something to trigger on."""
    out = []
    for index, price in enumerate(prices):
        price = float(price)
        out.append(
            Candle(
                ts=start_ts + index * ts_step,
                open=price,
                high=price * 1.01,
                low=price * 0.99,
                close=price,
                volume=100.0,
            )
        )
    return out


class SolvencyMixin:
    """Shared assertions about money that cannot exist."""

    def assert_solvent(self, result, label):
        for point in result.engine.portfolio.equity_curve:
            self.assertGreaterEqual(
                point.cash, -1e-6,
                f"{label}: cash went to {point.cash:.6f} - the account spent money it did not have",
            )
            self.assertGreater(
                point.equity, 0.0,
                f"{label}: equity fell to {point.equity:.6f} on a long-only run",
            )
            if point.equity > 0:
                exposure = (point.position * point.price) / point.equity
                self.assertLessEqual(
                    exposure, 1.0 + WEIGHT_TOLERANCE,
                    f"{label}: exposure reached {exposure:.6f} without leverage",
                )


class TestSolvencyAcrossStrategies(SolvencyMixin, unittest.TestCase):
    """No long-only strategy may overdraw the account, on any data, at any cost level."""

    def run_case(self, name, costs, candles, cash=1000.0):
        return backtest.run(
            candles,
            build(name),
            starting_cash=cash,
            costs=costs,
            limits=RiskLimits(**WIDE_OPEN),
            execution=ExecutionSettings(min_notional=1.0),
        )

    def test_no_strategy_overdraws_on_violent_data(self):
        candles = SyntheticFeed(bars=800, seed=99, volatility_per_bar=0.02).generate()
        costs = CostModel(taker_fee_bps=25, maker_fee_bps=25, half_spread_bps=10, slippage_bps=10)
        for name in available():
            with self.subTest(strategy=name):
                self.assert_solvent(self.run_case(name, costs, candles), name)

    def test_no_strategy_overdraws_when_a_flat_fee_applies(self):
        """The flat fee is the case that broke it: the fee itself creates the drift."""
        candles = SyntheticFeed(bars=400, seed=5, volatility_per_bar=0.01).generate()
        costs = CostModel(taker_fee_bps=10, maker_fee_bps=10, half_spread_bps=2,
                          slippage_bps=2, flat_fee=1.0)
        for name in available():
            with self.subTest(strategy=name):
                self.assert_solvent(self.run_case(name, costs, candles), name)

    def test_a_full_position_leaves_cash_at_zero_not_below(self):
        """Fully invested means exactly fully invested - not 100.4% funded by an overdraft."""
        result = self.run_case(
            "buy_and_hold",
            CostModel(taker_fee_bps=50, maker_fee_bps=50, half_spread_bps=20, slippage_bps=20),
            series([100.0] * 20),
        )
        held = [p for p in result.engine.portfolio.equity_curve if p.position > 0]
        self.assertTrue(held, "expected buy_and_hold to hold something")
        for point in held[:-1]:
            self.assertGreaterEqual(point.cash, -1e-9)
            self.assertLess(point.cash, 1.0, "cash should be spent, not idle")


class TestDegenerateSeries(unittest.TestCase):
    """A feed can return one bar, or a flat line. Neither may raise."""

    def run_all(self, candles):
        for name in available():
            with self.subTest(strategy=name):
                result = backtest.run(
                    candles, build(name), starting_cash=1000.0,
                    limits=RiskLimits(**WIDE_OPEN),
                    execution=ExecutionSettings(min_notional=1.0),
                )
                self.assertTrue(result.engine.portfolio.is_flat, "backtest must end flat")
                self.assertGreater(result.metrics.ending_equity, 0.0)

    def test_one_bar(self):
        self.run_all(series([100.0]))

    def test_two_bars(self):
        self.run_all(series([100.0, 100.5]))

    def test_a_perfectly_flat_market(self):
        self.run_all(series([100.0] * 50))

    def test_an_empty_series_is_refused_rather_than_guessed_at(self):
        with self.assertRaises(ValueError):
            backtest.run([], build("buy_and_hold"))


class TestAccountTooSmallToTrade(unittest.TestCase):
    """Below the venue's minimum, the right number of trades is zero."""

    def test_a_one_pound_account_places_no_orders_and_loses_nothing(self):
        candles = SyntheticFeed(bars=200, seed=3).generate()
        for name in available():
            with self.subTest(strategy=name):
                result = backtest.run(
                    candles, build(name), starting_cash=1.0,
                    limits=RiskLimits(**WIDE_OPEN),
                    execution=ExecutionSettings(min_notional=10.0),
                )
                self.assertEqual(result.metrics.trades, 0)
                self.assertAlmostEqual(result.metrics.ending_equity, 1.0, places=9)
                self.assertAlmostEqual(result.metrics.total_costs, 0.0, places=9)


class TestExtremeMoves(SolvencyMixin, unittest.TestCase):
    def test_a_ninety_nine_percent_crash_does_not_produce_negative_equity(self):
        prices = [100.0] * 30 + [1.0] * 30
        for name in available():
            with self.subTest(strategy=name):
                result = backtest.run(
                    series(prices), build(name), starting_cash=1000.0,
                    costs=CostModel(taker_fee_bps=10, maker_fee_bps=10,
                                    half_spread_bps=5, slippage_bps=5),
                    limits=RiskLimits(**WIDE_OPEN),
                    execution=ExecutionSettings(min_notional=1.0),
                )
                self.assert_solvent(result, name)

    def test_a_hundredfold_spike_does_not_break_the_books(self):
        prices = [100.0] * 30 + [10_000.0] * 30
        for name in available():
            with self.subTest(strategy=name):
                result = backtest.run(
                    series(prices), build(name), starting_cash=1000.0,
                    limits=RiskLimits(**WIDE_OPEN),
                    execution=ExecutionSettings(min_notional=1.0),
                )
                self.assert_solvent(result, name)

    def test_an_unmargined_short_can_lose_more_than_the_account(self):
        """Documenting a limitation, not asserting a feature.

        Shorting here is modelled with no margin call and no borrow cost, so a short
        into a spike runs the account past zero and keeps going. A real venue would
        have liquidated long before. This is why ``allow_short`` defaults to off, and
        why nothing above turns it on.
        """
        from tradebot.engine import Engine
        from tradebot.portfolio import Portfolio
        from tradebot.risk import RiskManager
        from tradebot.strategies.base import Strategy
        from tradebot.types import Decision

        class AlwaysShort(Strategy):
            name = "always_short"

            def on_candle(self, candle, ctx):
                return Decision(-1.0, reason="short")

        costs = CostModel(taker_fee_bps=0, maker_fee_bps=0, half_spread_bps=0, slippage_bps=0)
        limits = RiskLimits(**{**WIDE_OPEN, "allow_short": True})
        portfolio = Portfolio(starting_cash=1000.0)
        engine = Engine(
            strategy=AlwaysShort(),
            portfolio=portfolio,
            risk=RiskManager(limits=limits, costs=costs),
            costs=costs,
            execution=ExecutionSettings(min_notional=1.0),
        )
        for candle in series([100.0] * 5 + [10_000.0] * 5):
            engine.process(candle)

        self.assertLess(portfolio.equity(10_000.0), 0.0)

    def test_shorting_is_refused_by_default(self):
        """The protection against the case above is that the weight never gets through."""
        from tradebot.portfolio import Portfolio
        from tradebot.risk import RiskManager

        costs = CostModel()
        risk = RiskManager(limits=RiskLimits(**WIDE_OPEN), costs=costs)
        portfolio = Portfolio(starting_cash=1000.0)
        risk.observe(0, portfolio, 100.0)
        self.assertEqual(risk.evaluate(-1.0, portfolio, 100.0).target_weight, 0.0)


class TestCostsCannotRunAway(unittest.TestCase):
    """A position held at a fixed weight must not churn itself to death.

    Any target below 100% drifts as the price moves - hold 50% and a rally makes it
    52% - so an engine that corrects every drift trades on every single bar. Each
    correction pays a fee, and with a flat commission the fee moves the weight again,
    so the next bar has something new to correct. The two guards below cap how small
    a drift is worth fixing and how much fixing one may cost.

    This used to be demonstrated with buy-and-hold, which was wrong once the solvency
    fix landed: a fully-invested position now sits at exactly 1.0 with cash at zero,
    so there is no drift to chase and the guards never engage. The bug is still very
    much live at any other weight - unguarded, the run below pays 44% of the account
    in commission - so the test now uses the case that actually exercises it.
    """

    class HalfInvested(Strategy):
        name = "half_invested"

        def on_candle(self, candle, ctx):
            return Decision(0.5, reason="hold half")

    def run_half(self, execution):
        return backtest.run(
            SyntheticFeed(bars=2000, seed=11).generate(),
            self.HalfInvested(),
            starting_cash=10_000.0,
            costs=CostModel(taker_fee_bps=10, maker_fee_bps=10, half_spread_bps=2,
                            slippage_bps=2, flat_fee=2.0),
            limits=RiskLimits(**WIDE_OPEN),
            execution=execution,
        ).metrics

    def test_holding_a_fixed_weight_does_not_trade_every_bar(self):
        guarded = self.run_half(ExecutionSettings(min_notional=1.0))
        self.assertLess(
            guarded.trades, 100,
            "a fixed-weight hold over 2,000 bars should rebalance occasionally, not constantly",
        )
        self.assertLess(
            guarded.total_costs, 0.02 * 10_000.0,
            "costs above 2% of the account to hold one weight means it is churning",
        )

    def test_the_guards_are_what_prevents_it(self):
        """Both off, the same run is a fee pump. This is the regression being pinned."""
        unguarded = self.run_half(
            ExecutionSettings(min_notional=1.0, rebalance_threshold=0.0, max_resize_cost_share=1e9)
        )
        guarded = self.run_half(ExecutionSettings(min_notional=1.0))
        self.assertGreater(unguarded.trades, 10 * guarded.trades)
        self.assertGreater(unguarded.total_costs, 10 * guarded.total_costs)

    def test_the_threshold_is_what_protects_a_percentage_fee_account(self):
        """The other half of the pair, in the regime where the cost cap is inert.

        With no flat commission, a rebalance's cost is a fixed *fraction* of the amount
        it moves - 0.14% here - so the cost cap's "never pay more than 10% to move it"
        test passes no matter how small the move is, and waves through every one. Only
        the drift threshold stops a fixed-weight hold trading on all 2,000 bars.

        The costs barely differ between the two runs, which is the trap: judged on
        money alone this looks harmless. It is the trade count that gives it away, and
        on a venue with any per-order minimum or rate limit those trades are real.
        """
        proportional = dict(
            starting_cash=10_000.0,
            costs=CostModel(taker_fee_bps=10, maker_fee_bps=10, half_spread_bps=2, slippage_bps=2),
            limits=RiskLimits(**{**WIDE_OPEN, "min_trade_notional": 0.0}),
        )
        candles = SyntheticFeed(bars=2000, seed=11).generate()

        guarded = backtest.run(
            candles, self.HalfInvested(),
            execution=ExecutionSettings(min_notional=0.0), **proportional
        ).metrics
        no_threshold = backtest.run(
            candles, self.HalfInvested(),
            execution=ExecutionSettings(min_notional=0.0, rebalance_threshold=0.0), **proportional
        ).metrics

        self.assertLess(guarded.trades, 50)
        self.assertGreater(
            no_threshold.trades, 20 * guarded.trades,
            "without the drift threshold this should trade on almost every bar; "
            "if it no longer does, either the threshold is dead code or this test is wrong",
        )

    def test_the_cost_cap_is_what_protects_a_small_account(self):
        """The two guards are not redundant - they cover different account sizes.

        ``rebalance_threshold`` ignores drift below half a percent of equity, which on
        a large account is already more than the flat fee is worth. On a small one it
        is not: half a percent of £200 is a £1 rebalance paying a £2 commission, and
        the threshold happily waves it through. Only the cost cap refuses to pay more
        to move money than the money being moved.

        Unguarded, the run below spends £202 of a £200 account on commission. This is
        the guard that matters most to exactly the accounts most likely to run this.
        """
        small = dict(
            candles=SyntheticFeed(bars=2000, seed=11).generate(),
            starting_cash=200.0,
            costs=CostModel(taker_fee_bps=10, maker_fee_bps=10, half_spread_bps=2,
                            slippage_bps=2, flat_fee=2.0),
            limits=RiskLimits(**{**WIDE_OPEN, "min_trade_notional": 0.0}),
        )
        candles = small.pop("candles")

        # min_notional off, so the only thing standing between this account and ruin
        # is the cost cap itself.
        guarded = backtest.run(
            candles, self.HalfInvested(),
            execution=ExecutionSettings(min_notional=0.0), **small
        ).metrics
        uncapped = backtest.run(
            candles, self.HalfInvested(),
            execution=ExecutionSettings(min_notional=0.0, max_resize_cost_share=1e9), **small
        ).metrics

        self.assertLess(guarded.total_costs, 0.05 * 200.0)
        self.assertGreater(
            uncapped.total_costs, 200.0,
            "without the cap this run should spend more than the whole account on fees; "
            "if it no longer does, either the cap is dead code or this test is wrong",
        )


if __name__ == "__main__":
    unittest.main()
