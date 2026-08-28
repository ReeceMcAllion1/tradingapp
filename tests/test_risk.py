"""Risk manager tests.

These limits are the reason the system is safe to leave running. If any of these
tests fail, do not run the bot.
"""

from __future__ import annotations

import unittest

from tradebot.costs import CostModel
from tradebot.portfolio import Portfolio
from tradebot.risk import RiskLimits, RiskManager
from tradebot.types import Fill, Side

DAY_MS = 86_400_000


def manager(**overrides):
    limits = RiskLimits(**overrides)
    return RiskManager(limits=limits, costs=CostModel())


class TestPositionCaps(unittest.TestCase):
    def test_requested_weight_is_clamped_to_the_position_cap(self):
        risk = manager(max_position_pct=0.25)
        book = Portfolio(starting_cash=1000.0)
        verdict = risk.evaluate(1.0, book, price=100.0)
        self.assertAlmostEqual(verdict.target_weight, 0.25)

    def test_shorts_are_refused_unless_enabled(self):
        risk = manager(allow_short=False)
        book = Portfolio(starting_cash=1000.0)
        self.assertAlmostEqual(risk.evaluate(-1.0, book, 100.0).target_weight, 0.0)

    def test_shorts_are_allowed_when_enabled(self):
        risk = manager(allow_short=True, max_position_pct=0.5)
        book = Portfolio(starting_cash=1000.0)
        self.assertAlmostEqual(risk.evaluate(-1.0, book, 100.0).target_weight, -0.5)


class TestHalts(unittest.TestCase):
    def _losing_book(self, from_equity, to_equity):
        book = Portfolio(starting_cash=from_equity)
        book.cash = to_equity
        return book

    def test_daily_loss_limit_pauses_trading(self):
        risk = manager(max_daily_loss_pct=0.02)
        book = Portfolio(starting_cash=1000.0)

        risk.observe(0, book, 100.0)
        book.cash = 970.0  # down 3% on the day
        risk.observe(60_000, book, 100.0)

        self.assertEqual(risk.halted_reason, "daily loss limit")
        self.assertFalse(risk.evaluate(1.0, book, 100.0).approved)

    def test_daily_halt_lifts_on_the_next_day(self):
        risk = manager(max_daily_loss_pct=0.02)
        book = Portfolio(starting_cash=1000.0)

        risk.observe(0, book, 100.0)
        book.cash = 970.0
        risk.observe(60_000, book, 100.0)
        self.assertEqual(risk.halted_reason, "daily loss limit")

        risk.observe(DAY_MS + 60_000, book, 100.0)
        self.assertIsNone(risk.halted_reason)

    def test_drawdown_kill_switch_does_not_lift_on_a_new_day(self):
        """A 20% drawdown means the strategy is broken, not unlucky. It stays stopped."""
        risk = manager(max_drawdown_pct=0.20, max_daily_loss_pct=0.99)
        book = Portfolio(starting_cash=1000.0)

        risk.observe(0, book, 100.0)
        book.cash = 700.0
        risk.observe(60_000, book, 100.0)
        self.assertEqual(risk.halted_reason, "max drawdown")

        risk.observe(5 * DAY_MS, book, 100.0)
        self.assertEqual(risk.halted_reason, "max drawdown", "kill switch must persist")

    def test_closing_a_position_is_allowed_even_while_halted(self):
        """Risk limits must never trap you in a trade you are trying to exit."""
        risk = manager(max_drawdown_pct=0.20, max_daily_loss_pct=0.99)
        book = Portfolio(starting_cash=1000.0)
        book.apply(Fill(ts=0, side=Side.BUY, qty=1.0, price=100.0, fee=0.0, reference_price=100.0))

        risk.observe(0, book, 100.0)
        book.cash -= 300.0
        risk.observe(60_000, book, 100.0)
        self.assertIsNotNone(risk.halted_reason)

        verdict = risk.evaluate(0.0, book, 100.0)
        self.assertAlmostEqual(verdict.target_weight, 0.0, msg="must still be able to flatten")


class TestThrottles(unittest.TestCase):
    def test_daily_trade_cap_blocks_new_entries(self):
        risk = manager(max_trades_per_day=2)
        book = Portfolio(starting_cash=1000.0)
        risk.observe(0, book, 100.0)

        for _ in range(2):
            risk.record_trade_result(1.0)

        self.assertFalse(risk.evaluate(0.25, book, 100.0).approved)

    def test_cooldown_after_a_loss_blocks_the_next_few_bars(self):
        risk = manager(cooldown_bars_after_loss=2)
        book = Portfolio(starting_cash=1000.0)
        risk.observe(0, book, 100.0)
        risk.record_trade_result(-5.0)

        self.assertFalse(risk.evaluate(0.25, book, 100.0).approved)
        risk.observe(60_000, book, 100.0)
        risk.observe(120_000, book, 100.0)
        self.assertTrue(risk.evaluate(0.25, book, 100.0).approved)

    def test_a_win_does_not_trigger_a_cooldown(self):
        risk = manager(cooldown_bars_after_loss=3)
        book = Portfolio(starting_cash=1000.0)
        risk.observe(0, book, 100.0)
        risk.record_trade_result(5.0)
        self.assertTrue(risk.evaluate(0.25, book, 100.0).approved)


class TestEdgeFilter(unittest.TestCase):
    def test_a_tiny_expected_move_does_not_cover_costs(self):
        risk = RiskManager(limits=RiskLimits(min_edge_multiple=2.0), costs=CostModel())
        # Round trip is 0.28%; 2x that is 0.56%.
        self.assertFalse(risk.expected_edge_covers_costs(0.05))
        self.assertFalse(risk.expected_edge_covers_costs(0.30))
        self.assertTrue(risk.expected_edge_covers_costs(0.60))

    def test_a_zero_cost_venue_accepts_any_edge(self):
        free = CostModel(taker_fee_bps=0, maker_fee_bps=0, half_spread_bps=0, slippage_bps=0)
        risk = RiskManager(limits=RiskLimits(), costs=free)
        self.assertTrue(risk.expected_edge_covers_costs(0.0001))


class TestValidation(unittest.TestCase):
    def test_nonsense_limits_are_rejected_at_construction(self):
        for bad in ({"max_position_pct": 0}, {"max_position_pct": 1.5},
                    {"max_daily_loss_pct": 0}, {"max_drawdown_pct": 2.0},
                    {"min_trade_notional": -1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                RiskLimits(**bad)


if __name__ == "__main__":
    unittest.main()
