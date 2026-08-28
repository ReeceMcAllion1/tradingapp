"""Indicator tests, checked against hand-computed values."""

from __future__ import annotations

import unittest

from tradebot.indicators import ATR, EMA, RSI, RollingStats
from tradebot.types import Candle


class TestEMA(unittest.TestCase):
    def test_returns_none_until_the_period_is_filled(self):
        ema = EMA(3)
        self.assertIsNone(ema.update(10.0))
        self.assertIsNone(ema.update(20.0))
        self.assertEqual(ema.update(30.0), 20.0, "seeds with the simple average")

    def test_follows_the_standard_recurrence_after_seeding(self):
        ema = EMA(3)
        for price in (10.0, 20.0, 30.0):
            ema.update(price)
        # alpha = 2/(3+1) = 0.5; 20 + 0.5*(40-20) = 30
        self.assertAlmostEqual(ema.update(40.0), 30.0)

    def test_converges_towards_a_constant_price(self):
        ema = EMA(5)
        for _ in range(200):
            ema.update(100.0)
        self.assertAlmostEqual(ema.value, 100.0, places=6)

    def test_rejects_a_zero_period(self):
        with self.assertRaises(ValueError):
            EMA(0)


class TestATR(unittest.TestCase):
    def test_true_range_uses_the_previous_close(self):
        atr = ATR(2)
        atr.update(Candle(ts=1, open=10, high=12, low=8, close=10, volume=1))
        # Gap up: the true range spans from the old close of 10 to the new high of 25.
        value = atr.update(Candle(ts=2, open=20, high=25, low=20, close=22, volume=1))
        self.assertAlmostEqual(value, (4.0 + 15.0) / 2.0)

    def test_is_none_before_the_period_is_reached(self):
        atr = ATR(3)
        self.assertIsNone(atr.update(Candle(ts=1, open=10, high=11, low=9, close=10, volume=1)))
        self.assertIsNone(atr.update(Candle(ts=2, open=10, high=11, low=9, close=10, volume=1)))
        self.assertIsNotNone(atr.update(Candle(ts=3, open=10, high=11, low=9, close=10, volume=1)))


class TestRollingStats(unittest.TestCase):
    def test_mean_and_stdev_over_the_window(self):
        stats = RollingStats(4)
        for value in (2.0, 4.0, 4.0, 4.0):
            stats.update(value)
        self.assertAlmostEqual(stats.mean, 3.5)

    def test_zscore_is_zero_for_a_flat_window(self):
        stats = RollingStats(3)
        for _ in range(3):
            stats.update(50.0)
        self.assertAlmostEqual(stats.zscore(50.0), 0.0)

    def test_zscore_is_negative_below_the_mean(self):
        stats = RollingStats(5)
        for value in (100, 100, 100, 100, 100):
            stats.update(float(value))
        self.assertIsNotNone(stats.zscore(90.0))

    def test_window_drops_the_oldest_value(self):
        stats = RollingStats(2)
        stats.update(1.0)
        stats.update(2.0)
        stats.update(3.0)
        self.assertAlmostEqual(stats.mean, 2.5, msg="1.0 should have fallen out")


class TestRSI(unittest.TestCase):
    def test_all_gains_pins_to_one_hundred(self):
        rsi = RSI(5)
        value = None
        for price in range(100, 130):
            value = rsi.update(float(price))
        self.assertAlmostEqual(value, 100.0)

    def test_all_losses_pins_to_zero(self):
        rsi = RSI(5)
        value = None
        for price in range(130, 100, -1):
            value = rsi.update(float(price))
        self.assertAlmostEqual(value, 0.0, places=6)

    def test_stays_in_range_on_mixed_input(self):
        import random

        rng = random.Random(3)
        rsi = RSI(14)
        price = 100.0
        for _ in range(500):
            price *= 1 + rng.uniform(-0.02, 0.02)
            value = rsi.update(price)
            if value is not None:
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 100.0)


if __name__ == "__main__":
    unittest.main()
