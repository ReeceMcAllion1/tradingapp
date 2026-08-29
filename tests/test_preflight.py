"""Tests for the live-trading readiness checks.

These decide whether the software tells someone it is safe to risk real money, so
they matter more than most.
"""

from __future__ import annotations

import json
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


def toml_path(path) -> str:
    """A filesystem path as a TOML value, safe on every platform.

    A Windows path written straight into a double-quoted TOML string is not a path to
    the parser: the backslash starts an escape, ``\\U`` in ``C:\\Users`` begins a Unicode
    escape, and it fails with "Invalid hex value" pointing at a column that means
    nothing to the reader. This suite wrote paths that way and passed on Linux for
    exactly that reason - the bug only exists where the separator is a backslash.
    """
    return '"' + str(path).replace("\\", "\\\\") + '"'


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

    def test_a_stale_record_blocks(self):
        """A paper log from years ago is not evidence about today's market."""
        paper_csv(self.config.live.trades_file,
                  [("2020-01-01", "2020-03-15", 1.0) for _ in range(25)])
        check = self._named(preflight.run(self.config), "Record is current")
        self.assertFalse(check.passed)
        self.assertIn("market that has moved on", check.detail)

    def test_a_current_record_passes(self):
        from datetime import datetime, timedelta, timezone
        today = datetime.now(tz=timezone.utc)
        start = (today - timedelta(days=40)).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")
        paper_csv(self.config.live.trades_file, [(start, end, 1.0) for _ in range(25)])
        checks = preflight.run(self.config)
        self.assertTrue(self._named(checks, "Record is current").passed)
        self.assertTrue(self._named(checks, "Paper trading").passed)

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


class TestOrderCapConsequence(unittest.TestCase):
    """The cap trims rather than refuses, which is not what the backtest did."""

    def detail(self, cash, cap, position_pct):
        config = Config()
        config.account.starting_cash = cash
        config.live.max_order_notional = cap
        config.risk = RiskLimits(max_position_pct=position_pct)
        for check in preflight.run(config):
            if check.name == "Order size cap":
                return check.detail
        self.fail("no order size cap check")

    def test_it_says_how_many_orders_a_full_position_will_take(self):
        detail = self.detail(cash=1000.0, cap=50.0, position_pct=0.25)
        self.assertIn("5 orders", detail)
        self.assertIn("max_trades_per_day", detail)

    def test_nothing_is_said_when_the_cap_does_not_bite(self):
        detail = self.detail(cash=100.0, cap=1000.0, position_pct=0.25)
        self.assertNotIn("orders rather than one", detail)

    def test_a_cap_that_needs_more_orders_than_the_daily_limit_still_reports(self):
        """The worst case: the position can never be reached in a single day."""
        detail = self.detail(cash=100_000.0, cap=50.0, position_pct=1.0)
        self.assertIn("2000 orders", detail)


if __name__ == "__main__":
    unittest.main()


class TestCostDragCheck(unittest.TestCase):
    """Costs above a fifth of capital a year are fatal regardless of the signal."""

    def setUp(self):
        self.config = Config()
        self.config.live.trades_file = str(Path(tempfile.mkdtemp()) / "trades.csv")

    def _named(self, checks, name):
        return next(c for c in checks if c.name == name)

    def test_ruinous_cost_drag_blocks(self):
        checks = preflight.run(self.config, annual_cost_drag_pct=104.1)
        check = self._named(checks, "Cost drag")
        self.assertFalse(check.passed)
        self.assertTrue(check.blocking)
        self.assertIn("104.1%", check.detail)

    def test_modest_cost_drag_passes(self):
        checks = preflight.run(self.config, annual_cost_drag_pct=3.0)
        self.assertTrue(self._named(checks, "Cost drag").passed)

    def test_the_check_is_absent_when_not_measured(self):
        checks = preflight.run(self.config)
        self.assertFalse(any(c.name == "Cost drag" for c in checks))


class TestStatusReading(unittest.TestCase):
    """status must read a live session's files safely while it is still writing them."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = Config()
        self.config.live.state_file = str(self.tmp / "state.json")
        self.config.live.trades_file = str(self.tmp / "trades.csv")

    def _run_status(self, recent=5):
        import argparse
        import contextlib
        import io

        from tradebot import cli

        cfg_path = self.tmp / "c.toml"
        cfg_path.write_text(
            "[live]\n"
            f"state_file = {toml_path(self.config.live.state_file)}\n"
            f"trades_file = {toml_path(self.config.live.trades_file)}\n",
            encoding="utf-8",
        )
        args = argparse.Namespace(config=str(cfg_path), recent=recent)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_status(args)
        return out.getvalue()

    def test_it_reports_position_and_costs_from_the_state_file(self):
        import json

        (self.tmp / "state.json").write_text(json.dumps({
            "saved_at": 1_700_000_000_000, "symbol": "BTC_USD", "interval": "1m",
            "strategy": "never_lose",
            "engine": {"bars_seen": 42, "consecutive_rejections": 0,
                       "portfolio": {"qty": 0.5, "cash": 100.0, "avg_price": 1800.0,
                                     "fees_paid": 3.0, "slippage_paid": 1.0},
                       "risk": {"halted_reason": None}},
        }), encoding="utf-8")
        text = self._run_status()
        self.assertIn("never_lose on BTC_USD", text)
        self.assertIn("42", text)
        self.assertIn("4.00", text, "costs are fees plus slippage")

    def test_a_halt_is_reported_prominently(self):
        import json

        (self.tmp / "state.json").write_text(json.dumps({
            "saved_at": 1_700_000_000_000, "symbol": "BTC_USD", "strategy": "x",
            "engine": {"bars_seen": 1, "portfolio": {"qty": 0.0, "cash": 1.0},
                       "risk": {"halted_reason": "max drawdown"}},
        }), encoding="utf-8")
        self.assertIn("HALTED: max drawdown", self._run_status())

    def test_a_missing_session_exits_with_a_clear_message(self):
        with self.assertRaises(SystemExit) as caught:
            self._run_status()
        self.assertIn("no session state", str(caught.exception))

    def test_a_corrupt_state_file_exits_rather_than_traceback(self):
        (self.tmp / "state.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self._run_status()


class TestSessionReport(unittest.TestCase):
    """The end-of-run verdict must not misread a session that is fully invested."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = Config()
        self.config.live.state_file = str(self.tmp / "state.json")
        self.config.live.trades_file = str(self.tmp / "trades.csv")
        self.config.account.starting_cash = 1000.0

    def _write_state(self, **engine):
        import json
        base = {"bars_seen": 100, "portfolio": {"qty": 0.0, "cash": 1000.0,
                                                "fees_paid": 0.0, "slippage_paid": 0.0},
                "risk": {"halted_reason": None}}
        base.update(engine)
        (self.tmp / "state.json").write_text(json.dumps({
            "saved_at": 1_700_086_400_000, "started_at": 1_700_000_000_000,
            "symbol": "BTC_USD", "interval": "1h", "strategy": "x", "engine": base,
        }), encoding="utf-8")

    def test_an_open_position_is_marked_to_market(self):
        """A fully invested session holds no cash; reporting cash alone reads as ruin."""
        from tradebot import report as report_mod
        self._write_state(portfolio={"qty": 0.5, "cash": -1.0,
                                     "fees_paid": 1.0, "slippage_paid": 0.0})
        r = report_mod.load(self.config)
        self.assertAlmostEqual(r.equity, -1.0)
        report_mod.mark_to_market(r, price=2000.0)
        self.assertAlmostEqual(r.equity, 999.0)
        self.assertAlmostEqual(r.return_pct, -0.1, places=6)

    def test_the_span_comes_from_the_recorded_start_not_the_trade_date(self):
        from tradebot import report as report_mod
        self._write_state()
        r = report_mod.load(self.config)
        self.assertAlmostEqual(r.days, 1.0, places=3)

    def test_cost_drag_is_annualised_from_the_real_span(self):
        from tradebot import report as report_mod
        self._write_state(portfolio={"qty": 0.0, "cash": 1000.0,
                                     "fees_paid": 8.0, "slippage_paid": 2.0})
        r = report_mod.load(self.config)
        self.assertAlmostEqual(r.cost_drag_pct, 1.0)
        self.assertAlmostEqual(r.cost_drag_annual_pct, 365.0, places=0)

    def test_a_short_run_is_labelled_as_insufficient(self):
        from tradebot import report as report_mod
        self._write_state()
        text = report_mod.render([report_mod.load(self.config)])
        self.assertIn("far too short to judge", text)

    def test_a_missing_session_raises_rather_than_reporting_zeros(self):
        from tradebot import report as report_mod
        self.config.live.state_file = str(self.tmp / "nope.json")
        with self.assertRaises(FileNotFoundError):
            report_mod.load(self.config)

    def test_nothing_to_report_is_handled(self):
        from tradebot import report as report_mod
        self.assertIn("No sessions", report_mod.render([]))


class TestBarsProcessedMeansLiveBars(unittest.TestCase):
    """A session must not be credited with the warm-up it was handed at startup.

    The engine's bar counter carries the warm-up so its "do not trade on half-formed
    indicators" gate is satisfied by history rather than by waiting through it again
    live. Reporting that number as session activity tells you a run three bars old has
    processed a hundred and three.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.config = Config()
        self.config.live.state_file = str(self.tmp / "state.json")
        self.config.live.trades_file = str(self.tmp / "trades.csv")
        self.config.market.symbol = "BTC_USD"

    def write(self, **extra):
        payload = {
            "saved_at": 1_700_086_400_000, "started_at": 1_700_000_000_000,
            "symbol": "BTC_USD", "interval": "1m", "strategy": "vol_target",
            "engine": {"portfolio": {"qty": 0.0, "cash": 1000.0}, "risk": {},
                       "bars_seen": 103},
            **extra,
        }
        Path(self.config.live.state_file).write_text(json.dumps(payload), encoding="utf-8")

    def test_it_reports_the_bars_actually_traded(self):
        from tradebot import report as report_mod
        self.write(live_bars=3)
        self.assertEqual(report_mod.load(self.config).bars, 3)

    def test_a_state_file_without_the_count_falls_back_rather_than_failing(self):
        """Older state files predate the split; reading them must still work."""
        from tradebot import report as report_mod
        self.write()
        self.assertEqual(report_mod.load(self.config).bars, 103)


class TestSessionVerdict(unittest.TestCase):
    """The two figures a session is actually judged on.

    Both survived a mutation sweep - the "vs hold" gap could report the raw return
    instead of the difference, and the win count could count every trade as a winner,
    with the whole suite still green. Both fail in the flattering direction, which is
    the direction this package is meant to be paranoid about. The README tells people
    to read the "vs hold" column first; it had better be that column.
    """

    def session(self, **kwargs):
        from tradebot import report as report_mod

        defaults = dict(
            name="x", symbol="BTC_USD", interval="1h", strategy="x", bars=100,
            started=None, updated=None, equity=1100.0, position=0.0,
            starting_cash=1000.0, costs=5.0, trades=[],
        )
        return report_mod.SessionReport(**{**defaults, **kwargs})

    def test_the_gap_is_measured_against_the_benchmark(self):
        report = self.session(benchmark_return_pct=30.0)
        self.assertAlmostEqual(report.return_pct, 10.0)
        self.assertAlmostEqual(report.gap, -20.0, msg="10% against a 30% market is losing")

    def test_beating_the_market_gives_a_positive_gap(self):
        self.assertAlmostEqual(self.session(benchmark_return_pct=4.0).gap, 6.0)

    def test_no_benchmark_means_no_gap_rather_than_a_flattering_one(self):
        self.assertIsNone(self.session().gap)

    def test_a_losing_session_in_a_worse_market_still_beat_it(self):
        report = self.session(equity=900.0, benchmark_return_pct=-30.0)
        self.assertAlmostEqual(report.return_pct, -10.0)
        self.assertAlmostEqual(report.gap, 20.0)

    def test_only_profitable_trades_count_as_wins(self):
        trades = [{"net": "5.0"}, {"net": "-3.0"}, {"net": "0.0"}, {"net": "1.5"}]
        self.assertEqual(self.session(trades=trades).wins, 2)

    def test_a_break_even_trade_is_not_a_win(self):
        self.assertEqual(self.session(trades=[{"net": "0.0"}]).wins, 0)

    def test_an_unreadable_net_field_is_not_counted_as_a_win(self):
        """A malformed row must not inflate the record."""
        self.assertEqual(self.session(trades=[{"net": ""}, {}]).wins, 0)

    def test_all_losses_report_no_wins(self):
        self.assertEqual(self.session(trades=[{"net": "-1"}, {"net": "-2"}]).wins, 0)


class TestBenchmarkWindow(unittest.TestCase):
    """The benchmark must span the session's clock, not its bar count.

    A session that stalls - a suspended machine, a dropped feed - has far fewer bars
    than elapsed minutes. Sizing the window by bar count then compares a days-old
    position against a few minutes of market, which reads as a wild over- or
    under-performance that never happened.
    """

    def test_bars_needed_covers_elapsed_time_not_bar_count(self):
        from tradebot import report as report_mod

        r = report_mod.SessionReport(
            name="x", symbol="BTC_USD", interval="1m", strategy="x",
            bars=39, started=None, updated=None, equity=1000.0, position=0.0,
            starting_cash=1000.0, costs=0.0, trades=[],
        )
        # Four hours elapsed but only 39 bars recorded.
        from datetime import datetime, timedelta, timezone
        now = datetime.now(tz=timezone.utc)
        r.started = now - timedelta(hours=4)
        r.updated = now

        self.assertAlmostEqual(r.days, 4 / 24, places=3)
        needed = int(r.days * 24 * 60) + 10
        self.assertGreater(needed, 200, "must ask for ~240 bars, not 39")
        self.assertGreater(needed, r.bars * 5)
