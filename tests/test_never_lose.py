"""Tests for the never-sell-at-a-loss rule and the net break-even arithmetic."""

from __future__ import annotations

import unittest

from tradebot import backtest
from tradebot.costs import CostModel
from tradebot.engine import Engine, ExecutionSettings
from tradebot.portfolio import Portfolio
from tradebot.risk import RiskLimits, RiskManager
from tradebot.strategies import build
from tradebot.strategies.base import Context
from tradebot.types import Candle, Fill, Side

DAY = 86_400_000


def candle(day, open_, high, low, close):
    return Candle(ts=day * DAY, open=open_, high=high, low=low, close=close, volume=1.0)


class TestNetBreakevenExit(unittest.TestCase):
    def test_selling_at_the_entry_price_is_a_loss(self):
        """The whole reason the naive version fails: 'up a tick' is still down."""
        costs = CostModel()
        self.assertGreater(costs.net_breakeven_exit(100.0), 100.0)

    def test_a_round_trip_at_the_breakeven_price_nets_zero(self):
        for costs in (
            CostModel(),
            CostModel(taker_fee_bps=25, half_spread_bps=10, slippage_bps=5),
            CostModel(taker_fee_bps=0, half_spread_bps=0, slippage_bps=0),
        ):
            with self.subTest(costs=costs):
                book = Portfolio(starting_cash=100_000.0)
                entry_ref = 100.0
                entry_fill = costs.fill_price(Side.BUY, entry_ref)
                qty = 10.0
                book.apply(Fill(ts=0, side=Side.BUY, qty=qty, price=entry_fill,
                                fee=costs.fee(qty * entry_fill), reference_price=entry_ref))

                exit_ref = costs.net_breakeven_exit(entry_fill, qty)
                exit_fill = costs.fill_price(Side.SELL, exit_ref)
                closed = book.apply(Fill(ts=DAY, side=Side.SELL, qty=qty, price=exit_fill,
                                         fee=costs.fee(qty * exit_fill), reference_price=exit_ref))

                self.assertAlmostEqual(closed[0].net_pnl, 0.0, places=6)

    def test_a_flat_fee_raises_the_bar_and_needs_the_quantity(self):
        costs = CostModel(taker_fee_bps=0, half_spread_bps=0, slippage_bps=0, flat_fee=6.0)
        with_qty = costs.net_breakeven_exit(100.0, qty=10.0)
        without = costs.net_breakeven_exit(100.0)
        self.assertGreater(with_qty, without, "the flat fee must be covered too")
        self.assertAlmostEqual(with_qty, 100.0 + 12.0 / 10.0, places=6)

    def test_a_zero_cost_venue_breaks_even_at_the_entry_price(self):
        free = CostModel(taker_fee_bps=0, maker_fee_bps=0, half_spread_bps=0, slippage_bps=0)
        self.assertAlmostEqual(free.net_breakeven_exit(100.0), 100.0)


class TestNeverLoseBehaviour(unittest.TestCase):
    def _engine(self, cash=10_000.0, **params):
        costs = CostModel(taker_fee_bps=2, maker_fee_bps=2, half_spread_bps=1, slippage_bps=2)
        portfolio = Portfolio(starting_cash=cash)
        risk = RiskManager(
            limits=RiskLimits(max_position_pct=1.0, max_daily_loss_pct=0.99,
                              max_drawdown_pct=0.99, max_trades_per_day=10_000,
                              min_trade_notional=1.0, cooldown_bars_after_loss=0),
            costs=costs,
        )
        engine = Engine(
            strategy=build("never_lose", **params), portfolio=portfolio, risk=risk,
            costs=costs, execution=ExecutionSettings(min_notional=1.0),
        )
        return engine, portfolio, costs

    def test_it_never_sets_a_stop_loss(self):
        """No stop is not an oversight here, it is the definition of the strategy."""
        costs = CostModel()
        strategy = build("never_lose")
        flat = Context(exposure=0.0, equity=1000.0, costs=costs)
        held = Context(exposure=1.0, equity=1000.0, costs=costs, avg_price=100.0)

        self.assertIsNone(strategy.on_candle(candle(1, 100, 100, 100, 100), flat).stop_loss)
        self.assertIsNone(strategy.on_candle(candle(2, 50, 50, 50, 50), held).stop_loss)

    def test_it_holds_through_a_catastrophic_fall_without_selling(self):
        engine, portfolio, _ = self._engine()
        engine.process(candle(1, 100, 100, 100, 100))
        engine.process(candle(2, 100, 100, 100, 100))
        self.assertFalse(portfolio.is_flat)
        entry_qty = portfolio.qty

        for day in range(3, 40):  # a 90% collapse
            price = 100.0 * (1.0 - 0.9 * (day - 2) / 37.0)
            engine.process(candle(day, price, price, price, price))

        self.assertAlmostEqual(portfolio.qty, entry_qty, msg="it must not have sold a single share")
        self.assertEqual(len(portfolio.trades), 0, "no closed trades - the loss is never realised")

    def test_it_sells_once_the_trade_actually_nets_a_profit(self):
        engine, portfolio, costs = self._engine()
        engine.process(candle(1, 100, 100, 100, 100))
        engine.process(candle(2, 100, 100, 100, 100))

        target = costs.net_breakeven_exit(portfolio.avg_price)
        engine.process(candle(3, 100, target * 1.01, 99, target * 1.005))

        self.assertEqual(len(portfolio.trades), 1)
        self.assertGreaterEqual(portfolio.trades[0].net_pnl, 0.0, "an exit must never be a loss")

    def test_it_does_not_sell_for_a_gross_gain_that_is_a_net_loss(self):
        """Up a tick on price, still down after costs - it must keep holding."""
        engine, portfolio, _ = self._engine()
        engine.process(candle(1, 100, 100, 100, 100))
        engine.process(candle(2, 100, 100, 100, 100))

        # A high just above the entry, but below the cost-covering level.
        engine.process(candle(3, 100, portfolio.avg_price * 1.0005, 99, 100.2))
        self.assertEqual(len(portfolio.trades), 0, "that gain would not have covered costs")

    def test_the_gross_variant_does_book_that_losing_win(self):
        engine, portfolio, _ = self._engine(gross=True)
        engine.process(candle(1, 100, 100, 100, 100))
        engine.process(candle(2, 100, 100, 100, 100))
        engine.process(candle(3, 100, portfolio.avg_price * 1.0005, 99, 100.2))

        self.assertEqual(len(portfolio.trades), 1)
        self.assertLess(portfolio.trades[0].net_pnl, 0.0,
                        "the naive reading books a 'win' that loses money")

    def test_it_warns_that_its_win_rate_is_an_illusion(self):
        warnings = build("never_lose").cost_warnings(CostModel())
        self.assertTrue(any("never closed" in w for w in warnings))

    def test_a_negative_profit_target_is_rejected(self):
        with self.assertRaises(ValueError):
            build("never_lose", min_profit_pct=-0.01)


class TestTheWinRateIllusion(unittest.TestCase):
    """The central claim: a perfect win rate is trivially achievable and means nothing."""

    def _falling_then_flat(self):
        """Price halves and stays there - a stock that never recovers."""
        bars = [candle(d, 100, 100.5, 99.5, 100) for d in range(1, 4)]
        for d in range(4, 60):
            p = max(50.0, 100.0 - 1.0 * (d - 3))
            bars.append(candle(d, p, p * 1.002, p * 0.998, p))
        return bars

    def test_every_closed_trade_wins_while_the_account_loses(self):
        costs = CostModel(taker_fee_bps=2, maker_fee_bps=2, half_spread_bps=1, slippage_bps=2)
        limits = RiskLimits(max_position_pct=1.0, max_daily_loss_pct=0.99,
                            max_drawdown_pct=0.99, max_trades_per_day=10_000,
                            min_trade_notional=1.0, cooldown_bars_after_loss=0)
        result = backtest.run(self._falling_then_flat(), build("never_lose", min_profit_pct=0.001),
                              starting_cash=10_000.0, costs=costs, limits=limits,
                              execution=ExecutionSettings(min_notional=1.0))

        voluntary = [t for t in result.trades if t.reason != "end of backtest"]
        for trade in voluntary:
            self.assertGreater(trade.net_pnl, 0.0, "every voluntary exit is a winner")
        self.assertLess(result.metrics.net_pnl, 0.0, "and the account still lost money")


if __name__ == "__main__":
    unittest.main()
