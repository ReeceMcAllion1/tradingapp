"""Tests for volatility-targeted sizing.

This strategy exists because walk-forward said the others do not work. Across eight
markets and forty-eight folds, direction-timing beat buy-and-hold in ten folds and
lost to it on median in every single market; the one effect that survived everywhere
was a smaller drawdown. So this one stops forecasting direction and sizes by realised
volatility instead - it decides how much to hold, never whether to hold.

The tests below check the mechanism, not the profitability. Whether it makes money is
a question for `tradebot walkforward`, and no unit test can answer it.
"""

from __future__ import annotations

import math
import unittest

from tradebot.costs import CostModel
from tradebot.strategies import build
from tradebot.strategies.base import Context
from tradebot.strategies.vol_target import VolTarget
from tradebot.types import Candle

DAY_MS = 86_400_000
COSTS = CostModel()


def ctx(exposure=0.0, equity=1000.0):
    return Context(exposure=exposure, equity=equity, costs=COSTS)


def walk(strategy, prices, start_ts=1_700_000_000_000, step=DAY_MS):
    """Feed a price path and return every decision."""
    out = []
    for i, price in enumerate(prices):
        candle = Candle(ts=start_ts + i * step, open=price, high=price * 1.001,
                        low=price * 0.999, close=price, volume=1.0)
        out.append(strategy.on_candle(candle, ctx()))
    return out


def wobble(bars, amplitude, base=100.0):
    """A price path with a controlled amount of volatility and no drift."""
    return [base * (1.0 + amplitude * (-1) ** i) for i in range(bars)]


class TestSizingResponds(unittest.TestCase):
    def test_a_calm_market_is_held_at_full_size(self):
        decisions = walk(VolTarget(lookback=10, target_vol=0.15), wobble(60, 0.0002))
        self.assertAlmostEqual(decisions[-1].target_weight, 1.0)

    def test_a_violent_market_is_held_smaller(self):
        calm = walk(VolTarget(lookback=10, target_vol=0.15), wobble(60, 0.0002))[-1]
        wild = walk(VolTarget(lookback=10, target_vol=0.15), wobble(60, 0.05))[-1]
        self.assertLess(wild.target_weight, calm.target_weight)

    def test_size_falls_as_volatility_rises(self):
        """Monotonic: more turbulence must never mean a bigger position."""
        weights = [
            walk(VolTarget(lookback=10, target_vol=0.15), wobble(60, a))[-1].target_weight
            for a in (0.0002, 0.002, 0.01, 0.03, 0.08)
        ]
        for calmer, wilder in zip(weights, weights[1:]):
            self.assertLessEqual(wilder, calmer, f"{weights} is not monotonic")

    def test_it_never_asks_for_more_than_the_maximum(self):
        for amplitude in (0.0, 0.0001, 0.001, 0.05):
            decisions = walk(VolTarget(lookback=10, target_vol=0.15, max_weight=0.6),
                             wobble(60, amplitude))
            for d in decisions:
                if d.target_weight is not None:
                    self.assertLessEqual(d.target_weight, 0.6 + 1e-12)

    def test_a_wild_enough_market_is_not_held_at_all(self):
        decisions = walk(VolTarget(lookback=10, target_vol=0.05, step=0.2), wobble(60, 0.30))
        self.assertAlmostEqual(decisions[-1].target_weight, 0.0)


class TestItForecastsNothing(unittest.TestCase):
    """The design claim: direction is never consulted. Only magnitude."""

    def test_a_rising_and_a_falling_market_of_equal_violence_size_the_same(self):
        rise = [100.0 * 1.01**i for i in range(60)]
        fall = [100.0 * 0.99**i for i in range(60)]
        up = walk(VolTarget(lookback=20, target_vol=0.15), rise)[-1]
        down = walk(VolTarget(lookback=20, target_vol=0.15), fall)[-1]
        self.assertAlmostEqual(up.target_weight, down.target_weight, places=9)

    def test_it_stays_invested_through_a_decline(self):
        """A trend filter sells; this only shrinks. That difference is the whole point."""
        decisions = walk(VolTarget(lookback=20, target_vol=0.60), [100.0 * 0.99**i for i in range(60)])
        self.assertGreater(decisions[-1].target_weight, 0.0)


class TestCostControl(unittest.TestCase):
    """Cost drag is what killed every other strategy here, so it is a first-class test."""

    def test_the_weight_is_quantised_to_the_step(self):
        for d in walk(VolTarget(lookback=10, target_vol=0.15, step=0.25), wobble(80, 0.01)):
            if d.target_weight is not None:
                rungs = d.target_weight / 0.25
                self.assertAlmostEqual(rungs, round(rungs), places=9,
                                       msg=f"{d.target_weight} is not on a 0.25 rung")

    def test_a_coarser_step_changes_its_mind_less_often(self):
        """On a market whose turbulence drifts, a fine step chases it and pays for it.

        The path below breathes between calm and violent rather than wobbling at a
        constant rate - a steady market produces a steady weight whatever the step,
        which tests nothing.
        """
        prices = [100.0]
        for i in range(400):
            amplitude = 0.002 + 0.03 * (0.5 + 0.5 * math.sin(i / 40.0))
            prices.append(prices[-1] * (1.0 + amplitude * (-1) ** i))

        changes = {}
        for step in (0.05, 0.2, 0.5):
            weights = [d.target_weight for d in walk(VolTarget(lookback=20, step=step), prices)]
            changes[step] = sum(1 for a, b in zip(weights, weights[1:]) if a != b)

        self.assertGreater(changes[0.05], 2, f"the fixture is not exercising sizing: {changes}")
        self.assertLess(changes[0.5], changes[0.05],
                        f"a coarser step must trade less: {changes}")

    def test_quantising_rounds_down_so_risk_is_never_exceeded(self):
        """Rounding a risk target up would hold more than the target allows."""
        decisions = walk(VolTarget(lookback=10, target_vol=0.15, step=0.3), wobble(60, 0.004))
        for d in decisions:
            if d.target_weight is not None:
                self.assertLessEqual(d.target_weight, 1.0)

    def test_a_hair_trigger_step_warns_about_its_own_costs(self):
        notes = VolTarget(step=0.02).cost_warnings(CostModel())
        self.assertTrue(notes)
        self.assertIn("Cost drag", notes[0])

    def test_a_sensible_step_does_not_nag(self):
        self.assertEqual(VolTarget(step=0.25).cost_warnings(CostModel()), [])


class TestOptionalTrendFilter(unittest.TestCase):
    def test_disabled_by_default_so_the_sizing_is_tested_alone(self):
        """A direction call bolted on by default would take credit for the sizing."""
        self.assertIsNone(VolTarget().trend)

    def test_enabled_it_goes_to_cash_below_the_average(self):
        strategy = VolTarget(lookback=10, trend_period=20, target_vol=0.60)
        rising = [100.0 * 1.01**i for i in range(60)]
        crash = [rising[-1] * 0.90**i for i in range(30)]
        decisions = walk(strategy, rising + crash)
        self.assertAlmostEqual(decisions[-1].target_weight, 0.0)


class TestTimeframeIndependence(unittest.TestCase):
    """Volatility must be annualised from the real bar spacing, not an assumed one."""

    def test_the_same_path_on_different_bar_sizes_annualises_differently(self):
        path = wobble(60, 0.01)
        daily = walk(VolTarget(lookback=20), path, step=DAY_MS)[-1]
        hourly = walk(VolTarget(lookback=20), path, step=DAY_MS // 24)[-1]
        self.assertLessEqual(hourly.target_weight, daily.target_weight,
                             "the same wobble packed into hours is far more volatile")


class TestRejectsNonsense(unittest.TestCase):
    def test_bad_parameters_are_refused(self):
        for kwargs in ({"lookback": 1}, {"target_vol": 0}, {"target_vol": -0.1},
                       {"max_weight": 0}, {"max_weight": 1.5}, {"step": 0},
                       {"step": 1.5}, {"trend_period": 1}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    VolTarget(**kwargs)

    def test_it_holds_nothing_until_it_has_seen_enough(self):
        decisions = walk(VolTarget(lookback=30), wobble(10, 0.01))
        for d in decisions:
            self.assertEqual(d.target_weight, 0.0)
            self.assertEqual(d.reason, "warming up")

    def test_it_is_registered_and_buildable(self):
        self.assertIsInstance(build("vol_target"), VolTarget)


if __name__ == "__main__":
    unittest.main()
