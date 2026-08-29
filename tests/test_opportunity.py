"""What the same money would have done in a plain index fund.

This comparison exists because it keeps winning. Over the decade this repository tests
on, ten thousand pounds left in SPY finished around forty-one thousand and the best
strategy built here finished at twenty-seven - and the largest financial risk anyone
running this faces is not a bad trade, it is funding the thing under the impression it
is winning when it is not. So the alternative is priced in pounds and printed next to
the result.

The tests below are mostly about not lying in either direction: the index is charged
its own fee, it is never credited with a window it did not cover, and when it loses the
output says so.
"""

from __future__ import annotations

import unittest

from tradebot import opportunity
from tradebot.types import Candle

DAY = 86_400_000
YEAR_BARS = 366


def series(prices, step=DAY):
    return [
        Candle(ts=i * step, open=p, high=p * 1.01, low=p * 0.99, close=p, volume=1.0)
        for i, p in enumerate(map(float, prices))
    ]


class TestMeasuring(unittest.TestCase):
    def test_a_doubling_index_doubles_the_money(self):
        o = opportunity.measure(series([100, 200]), starting_cash=1000.0)
        self.assertAlmostEqual(o.gross_return_pct, 100.0)
        self.assertAlmostEqual(o.ending_cash, 2000.0, places=0)

    def test_it_charges_the_tracker_its_own_annual_fee(self):
        """A yardstick that got a free ride would be doing the thing this repo complains about."""
        flat = series([100.0] * YEAR_BARS)
        o = opportunity.measure(flat, starting_cash=10_000.0)
        self.assertAlmostEqual(o.gross_return_pct, 0.0, places=9)
        self.assertLess(o.return_pct, 0.0, "a year of holding must cost the tracker fee")
        self.assertAlmostEqual(o.return_pct, -opportunity.INDEX_ANNUAL_FEE * 100.0, places=2)

    def test_the_fee_scales_with_the_holding_period(self):
        one = opportunity.measure(series([100.0] * YEAR_BARS), 10_000.0)
        five = opportunity.measure(series([100.0] * (YEAR_BARS * 5)), 10_000.0)
        self.assertLess(five.return_pct, one.return_pct)

    def test_too_little_data_prices_nothing_rather_than_guessing(self):
        self.assertIsNone(opportunity.measure([], 1000.0))
        self.assertIsNone(opportunity.measure(series([100]), 1000.0))

    def test_a_worthless_price_cannot_reach_this_at_all(self):
        """The guard in measure() is belt-and-braces: Candle refuses it first.

        Worth pinning, because the reason the divide-by-zero cannot happen is the type
        rather than the arithmetic, and that is the sort of protection a later
        refactor removes without noticing.
        """
        with self.assertRaises(ValueError):
            Candle(ts=0, open=0.0, high=0.0, low=0.0, close=0.0, volume=1.0)


class TestTheShortfall(unittest.TestCase):
    """The number that decides whether the activity was worth doing."""

    def test_it_is_positive_when_the_index_won(self):
        o = opportunity.measure(series([100, 200]), starting_cash=1000.0)
        self.assertGreater(o.shortfall(strategy_ending=1500.0), 0.0)

    def test_it_is_negative_when_the_strategy_won(self):
        o = opportunity.measure(series([100, 200]), starting_cash=1000.0)
        self.assertLess(o.shortfall(strategy_ending=2500.0), 0.0)

    def test_it_is_measured_in_money(self):
        o = opportunity.measure(series([100, 200]), starting_cash=1000.0)
        self.assertAlmostEqual(o.shortfall(1500.0), o.ending_cash - 1500.0, places=9)


class TestWhatItSays(unittest.TestCase):
    def test_losing_to_the_index_is_stated_in_pounds(self):
        o = opportunity.measure(series([100, 200]), starting_cash=1000.0)
        text = opportunity.render(o, strategy_ending=1500.0, strategy_label="vol_target")
        self.assertIn("Trading cost you", text)
        self.assertIn("500", text)
        self.assertIn("vol_target", text)

    def test_beating_the_index_is_not_oversold(self):
        o = opportunity.measure(series([100, 110]), starting_cash=1000.0)
        text = opportunity.render(o, strategy_ending=2000.0)
        self.assertIn("beat the index", text)
        self.assertIn("out of sample", text,
                      "a single winning window must come with the usual warning")

    def test_a_long_window_warns_that_the_index_is_not_safe(self):
        o = opportunity.measure(series([100.0] * YEAR_BARS), starting_cash=1000.0)
        text = opportunity.render(o, strategy_ending=900.0)
        self.assertIn("not a safe asset", text)

    def test_nothing_to_compare_prints_nothing(self):
        """A missing index must leave the real result alone, not emit an empty box."""
        self.assertEqual(opportunity.render(None, strategy_ending=1000.0), "")

    def test_it_names_the_index_it_used(self):
        o = opportunity.measure(series([100, 120]), starting_cash=1000.0)
        self.assertIn(opportunity.DEFAULT_INDEX, opportunity.render(o, 1000.0))


class TestItDoesNotFlatterEitherSide(unittest.TestCase):
    def test_an_index_that_fell_is_reported_as_falling(self):
        o = opportunity.measure(series([200, 100]), starting_cash=1000.0)
        self.assertLess(o.return_pct, -49.0)
        self.assertAlmostEqual(o.ending_cash, 500.0, places=0)

    def test_beating_a_falling_index_still_counts_as_beating_it(self):
        o = opportunity.measure(series([200, 100]), starting_cash=1000.0)
        text = opportunity.render(o, strategy_ending=800.0)
        self.assertIn("beat the index", text,
                      "losing less than the index is still beating it")

    def test_the_strategy_return_shown_is_its_own(self):
        o = opportunity.measure(series([100, 200]), starting_cash=1000.0)
        text = opportunity.render(o, strategy_ending=1250.0)
        self.assertIn("25.0%", text)


if __name__ == "__main__":
    unittest.main()
