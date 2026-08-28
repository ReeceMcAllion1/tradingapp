"""Accounting tests.

If the portfolio is wrong, everything downstream is wrong in a way that looks
plausible, so these tests check the arithmetic rather than just the plumbing.
"""

from __future__ import annotations

import unittest

from tradebot.portfolio import Portfolio
from tradebot.types import Fill, Side


def fill(side, qty, price, fee=0.0, ts=0, reference=None):
    return Fill(
        ts=ts,
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        reference_price=price if reference is None else reference,
    )


class TestPortfolio(unittest.TestCase):
    def test_buy_reduces_cash_by_notional_plus_fee(self):
        book = Portfolio(starting_cash=1000.0)
        book.apply(fill(Side.BUY, 2.0, 100.0, fee=1.5))
        self.assertAlmostEqual(book.cash, 1000.0 - 200.0 - 1.5)
        self.assertAlmostEqual(book.qty, 2.0)
        self.assertAlmostEqual(book.avg_price, 100.0)

    def test_equity_is_flat_across_a_fee_free_purchase(self):
        book = Portfolio(starting_cash=1000.0)
        book.apply(fill(Side.BUY, 1.0, 100.0))
        self.assertAlmostEqual(book.equity(100.0), 1000.0)

    def test_average_price_blends_on_a_second_buy(self):
        book = Portfolio(starting_cash=1000.0)
        book.apply(fill(Side.BUY, 1.0, 100.0))
        book.apply(fill(Side.BUY, 1.0, 120.0))
        self.assertAlmostEqual(book.avg_price, 110.0)
        self.assertAlmostEqual(book.qty, 2.0)

    def test_round_trip_produces_one_trade_with_correct_pnl(self):
        book = Portfolio(starting_cash=1000.0)
        book.apply(fill(Side.BUY, 1.0, 100.0, fee=1.0))
        closed = book.apply(fill(Side.SELL, 1.0, 110.0, fee=1.1))

        self.assertEqual(len(closed), 1)
        trade = closed[0]
        self.assertAlmostEqual(trade.executed_pnl, 10.0)
        self.assertAlmostEqual(trade.fees, 2.1)
        self.assertAlmostEqual(trade.net_pnl, 7.9)
        self.assertAlmostEqual(book.cash, 1000.0 + 7.9)
        self.assertTrue(book.is_flat)

    def test_partial_close_leaves_the_remainder_open(self):
        book = Portfolio(starting_cash=1000.0)
        book.apply(fill(Side.BUY, 2.0, 100.0, fee=2.0))
        closed = book.apply(fill(Side.SELL, 1.0, 110.0, fee=1.1))

        self.assertEqual(len(closed), 1)
        self.assertAlmostEqual(closed[0].qty, 1.0)
        self.assertAlmostEqual(book.qty, 1.0)
        self.assertAlmostEqual(book.avg_price, 100.0)
        # Half the entry fee belongs to the half that closed.
        self.assertAlmostEqual(closed[0].fees, 1.0 + 1.1)

    def test_flip_from_long_to_short_closes_then_reopens(self):
        book = Portfolio(starting_cash=1000.0)
        book.apply(fill(Side.BUY, 1.0, 100.0, fee=1.0))
        closed = book.apply(fill(Side.SELL, 3.0, 110.0, fee=3.3))

        self.assertEqual(len(closed), 1)
        self.assertAlmostEqual(closed[0].qty, 1.0)
        self.assertAlmostEqual(closed[0].executed_pnl, 10.0)
        self.assertAlmostEqual(book.qty, -2.0, msg="should now be short the remainder")
        self.assertAlmostEqual(book.avg_price, 110.0)
        # Only a third of the exit fee belonged to the closing part.
        self.assertAlmostEqual(closed[0].fees, 1.0 + 1.1)

    def test_short_round_trip_profits_when_price_falls(self):
        book = Portfolio(starting_cash=1000.0)
        book.apply(fill(Side.SELL, 1.0, 100.0))
        closed = book.apply(fill(Side.BUY, 1.0, 90.0))
        self.assertAlmostEqual(closed[0].executed_pnl, 10.0)
        self.assertAlmostEqual(book.cash, 1010.0)

    def test_slippage_is_separated_from_the_market_move(self):
        book = Portfolio(starting_cash=1000.0)
        # Mid was 100, we paid 101. Mid was 110 on exit, we got 109.
        book.apply(fill(Side.BUY, 1.0, 101.0, reference=100.0))
        closed = book.apply(fill(Side.SELL, 1.0, 109.0, reference=110.0))

        trade = closed[0]
        self.assertAlmostEqual(trade.gross_pnl, 10.0, msg="the market moved 10")
        self.assertAlmostEqual(trade.executed_pnl, 8.0, msg="we captured 8 of it")
        self.assertAlmostEqual(trade.slippage_cost, 2.0, msg="2 went to spread")

    def test_cash_is_conserved_over_many_random_round_trips(self):
        """Starting cash plus every realised net P&L must equal the final cash."""
        import random

        rng = random.Random(11)
        book = Portfolio(starting_cash=10_000.0)
        realised = 0.0
        for i in range(300):
            price = 100.0 * (1.0 + rng.uniform(-0.2, 0.2))
            qty = round(rng.uniform(0.1, 2.0), 4)
            side = Side.BUY if rng.random() < 0.5 else Side.SELL
            for trade in book.apply(fill(side, qty, price, fee=qty * price * 0.001, ts=i)):
                realised += trade.net_pnl

        # Flatten so nothing is left marked to market.
        if not book.is_flat:
            side = Side.SELL if book.qty > 0 else Side.BUY
            for trade in book.apply(fill(side, abs(book.qty), 100.0, ts=999)):
                realised += trade.net_pnl

        self.assertAlmostEqual(book.cash, 10_000.0 + realised, places=6)

    def test_state_round_trips_through_save_and_restore(self):
        book = Portfolio(starting_cash=1000.0)
        book.apply(fill(Side.BUY, 1.5, 100.0, fee=1.0, ts=42, reference=99.5))

        restored = Portfolio(starting_cash=1000.0)
        restored.restore(book.state())

        self.assertAlmostEqual(restored.cash, book.cash)
        self.assertAlmostEqual(restored.qty, book.qty)
        self.assertAlmostEqual(restored.avg_price, book.avg_price)
        self.assertAlmostEqual(restored._open_reference, book._open_reference)

    def test_zero_quantity_fill_is_rejected(self):
        book = Portfolio(starting_cash=1000.0)
        with self.assertRaises(ValueError):
            book.apply(fill(Side.BUY, 0.0, 100.0))


if __name__ == "__main__":
    unittest.main()
