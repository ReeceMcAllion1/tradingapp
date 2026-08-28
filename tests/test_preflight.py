"""Tests for the live-trading readiness checks.

These decide whether the software tells someone it is safe to risk real money, so
they matter more than most.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tradebot import preflight
from tradebot.config import Config
from tradebot.risk import RiskLimits


def paper_csv(path, rows):
    lines = ["n,opened,closed,days,side,qty,entry,exit,gross,costs,net,balance,reason"]
    for i, (opened, closed, net) in enumerate(rows, start=1):
        lines.append(f"{i},{opened},{closed},1,buy,1,100,101,1,0.1,{net},1000,test")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestPreflight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = Config()
        self.config.live.trades_file = str(Path(self.tmp) / "trades.csv")

    def _named(self, checks, name):
        return next(c for c in checks if c.name == name)

    def test_no_paper_history_blocks(self):
        checks = preflight.run(self.config)
        check = self._named(checks, "Paper trading")
        self.assertFalse(check.passed)
        self.assertTrue(check.blocking)
        self.assertIn("no paper trades", check.detail)

    def test_a_short_paper_run_blocks(self):
        paper_csv(self.config.live.trades_file,
                  [("2026-01-01", "2026-01-02", 1.0) for _ in range(5)])
        check = self._named(preflight.run(self.config), "Paper trading")
        self.assertFalse(check.passed)
        self.assertIn("5 trades", check.detail)

    def test_a_long_enough_paper_run_passes(self):
        rows = [(f"2026-01-{(i % 28) + 1:02d}", "2026-03-15", 1.0) for i in range(25)]
        paper_csv(self.config.live.trades_file, rows)
        self.assertTrue(self._named(preflight.run(self.config), "Paper trading").passed)

    def test_losing_paper_trading_blocks(self):
        rows = [(f"2026-01-{(i % 28) + 1:02d}", "2026-03-15", -5.0) for i in range(25)]
        paper_csv(self.config.live.trades_file, rows)
        check = self._named(preflight.run(self.config), "Paper result")
        self.assertFalse(check.passed)
        self.assertIn("will not start winning on real ones", check.detail)

    def test_losing_to_buy_and_hold_blocks(self):
        checks = preflight.run(self.config, backtest_verdict=(False, "holding won"))
        check = self._named(checks, "Beats buy-and-hold")
        self.assertFalse(check.passed)
        self.assertTrue(check.blocking)

    def test_beating_buy_and_hold_passes(self):
        checks = preflight.run(self.config, backtest_verdict=(True, "beat it"))
        self.assertTrue(self._named(checks, "Beats buy-and-hold").passed)

    def test_missing_credentials_block(self):
        saved = {k: os.environ.pop(k, None)
                 for k in ("CRYPTOCOM_API_KEY", "CRYPTOCOM_API_SECRET")}
        try:
            check = self._named(preflight.run(self.config), "API credentials")
            self.assertFalse(check.passed)
            self.assertIn("never put them in config.toml", check.detail)
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value

    def test_loose_risk_limits_block(self):
        self.config.risk = RiskLimits(max_position_pct=1.0, max_drawdown_pct=0.9)
        check = self._named(preflight.run(self.config), "Risk limits")
        self.assertFalse(check.passed)
        self.assertIn("too loose", check.detail)

    def test_sensible_risk_limits_pass(self):
        self.assertTrue(self._named(preflight.run(self.config), "Risk limits").passed)

    def test_an_oversized_order_cap_warns_without_blocking(self):
        self.config.account.starting_cash = 100.0
        self.config.live.max_order_notional = 5_000.0
        check = self._named(preflight.run(self.config), "Order size cap")
        self.assertFalse(check.passed)
        self.assertFalse(check.blocking, "size is advisory, not a hard gate")

    def test_the_durable_host_check_is_always_raised(self):
        check = self._named(preflight.run(self.config), "Durable host")
        self.assertFalse(check.passed)
        self.assertFalse(check.blocking)
        self.assertIn("stays up", check.detail)

    def test_the_report_names_every_blocking_issue(self):
        text = preflight.render(preflight.run(self.config, backtest_verdict=(False, "no")))
        self.assertIn("Do not trade real money", text)
        self.assertIn("blocking issue", text)

    def test_a_clean_report_still_refuses_to_promise_profit(self):
        checks = [preflight.Check("All good", True, "fine")]
        text = preflight.render(checks)
        self.assertIn("No blocking issues", text)
        self.assertIn("not a prediction that you will make money", text)

    def test_a_corrupt_trade_file_does_not_crash_the_check(self):
        Path(self.config.live.trades_file).write_text("not,a,valid\x00csv", encoding="utf-8")
        checks = preflight.run(self.config)
        self.assertTrue(any(c.name == "Paper trading" for c in checks))


if __name__ == "__main__":
    unittest.main()
