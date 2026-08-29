"""The local dashboard: what it reads, what it refuses to guess, and what it never does.

A viewer has two jobs beyond looking right. It must never damage the thing it is
watching - it only ever reads, so stopping or starting it cannot disturb a running
session - and it must never invent a number, because a dashboard is exactly where an
invented number gets believed.

The second one is not hypothetical. Valuing an open position at the last closed trade's
exit price looked reasonable and was wrong: that price can be hours stale, or belong to
a different run whose trade log was copied alongside. It showed a £20 profit that had
never happened, which is how it was found.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tradebot import dashboard


def session(tmp: Path, name="s", cash=1000.0):
    return dashboard.Session(
        name=name, state_file=tmp / "state.json", trades_file=tmp / "trades.csv",
        starting_cash=cash, currency="£",
    )


def write_state(tmp: Path, **engine_extra):
    portfolio = {"cash": 500.0, "qty": 0.01, "avg_price": 50_000.0,
                 "fees_paid": 1.5, "slippage_paid": 0.5}
    portfolio.update(engine_extra.pop("portfolio", {}))
    payload = {
        "saved_at": 1_700_000_600_000, "started_at": 1_700_000_000_000,
        "symbol": "BTC_USD", "interval": "1m", "strategy": "vol_target",
        "live_bars": 7,
        "engine": {"portfolio": portfolio, "risk": {}, **engine_extra},
    }
    payload.update(engine_extra.pop("top", {}))
    (tmp / "state.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


class DashboardTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())


class TestValuingAnOpenPosition(DashboardTestCase):
    def test_it_marks_at_the_price_the_session_saw(self):
        payload = write_state(self.tmp)
        payload["last_price"] = 60_000.0
        (self.tmp / "state.json").write_text(json.dumps(payload), encoding="utf-8")

        s = dashboard.snapshot([session(self.tmp)])["sessions"][0]
        self.assertTrue(s["marked"])
        self.assertAlmostEqual(s["mark"], 60_000.0)
        self.assertAlmostEqual(s["equity"], 500.0 + 0.01 * 60_000.0)

    def test_a_stale_trade_log_cannot_set_the_price(self):
        """The bug: a foreign or hours-old trade log valuing a live position."""
        payload = write_state(self.tmp)
        payload["last_price"] = 60_000.0
        (self.tmp / "state.json").write_text(json.dumps(payload), encoding="utf-8")
        (self.tmp / "trades.csv").write_text(
            "n,opened,closed,days,side,qty,entry,exit,gross,costs,net,balance,reason\n"
            "1,2020-01-01,2020-01-02,1.0,buy,0.01,10,99999,1,0,1,1001,from another run\n",
            encoding="utf-8")

        s = dashboard.snapshot([session(self.tmp)])["sessions"][0]
        self.assertAlmostEqual(s["mark"], 60_000.0, msg="a foreign trade log set the mark")
        self.assertLess(s["equity"], 2000.0)

    def test_an_unmarked_position_is_refused_rather_than_guessed(self):
        write_state(self.tmp)          # no last_price at all
        s = dashboard.snapshot([session(self.tmp)])["sessions"][0]
        self.assertFalse(s["marked"], "valued a position with no price to value it at")

    def test_an_unmarked_position_falls_back_to_its_entry_not_to_zero(self):
        """The page hides the figure, but the API still hands it to whatever asks.

        Zero would report a held position as a total loss - the most alarming possible
        wrong answer, and one a reader has no way to recognise as a placeholder. The
        entry price at least says "worth what was paid for it", which is defensible
        while a fresh price is pending.
        """
        write_state(self.tmp)          # qty 0.01 at 50,000, no last_price
        s = dashboard.snapshot([session(self.tmp)])["sessions"][0]
        self.assertAlmostEqual(s["mark"], 50_000.0)
        self.assertAlmostEqual(s["equity"], 500.0 + 0.01 * 50_000.0)
        self.assertGreater(s["equity"], 0.0, "a held position was valued at nothing")

    def test_a_flat_session_needs_no_mark(self):
        write_state(self.tmp, portfolio={"qty": 0.0, "cash": 1000.0})
        s = dashboard.snapshot([session(self.tmp)])["sessions"][0]
        self.assertTrue(s["marked"])
        self.assertAlmostEqual(s["equity"], 1000.0)


class TestItSurvivesBadInput(DashboardTestCase):
    def test_a_session_that_has_not_started_is_reported_as_waiting(self):
        s = dashboard.snapshot([session(self.tmp)])["sessions"][0]
        self.assertTrue(s["waiting"])
        self.assertEqual(s["name"], "s")

    def test_a_half_written_state_file_does_not_crash_the_page(self):
        """State is written then moved, but a reader must survive seeing a partial file."""
        (self.tmp / "state.json").write_text("{not json", encoding="utf-8")
        s = dashboard.snapshot([session(self.tmp)])["sessions"][0]
        self.assertTrue(s["waiting"])

    def test_an_unreadable_trade_log_leaves_the_rest_intact(self):
        payload = write_state(self.tmp)
        payload["last_price"] = 60_000.0
        (self.tmp / "state.json").write_text(json.dumps(payload), encoding="utf-8")
        (self.tmp / "trades.csv").write_text("\x00\x00 garbage", encoding="utf-8")
        s = dashboard.snapshot([session(self.tmp)])["sessions"][0]
        self.assertFalse(s["waiting"])
        self.assertAlmostEqual(s["equity"], 500.0 + 0.01 * 60_000.0)

    def test_a_halt_is_surfaced(self):
        write_state(self.tmp, risk={"halted_reason": "max drawdown"})
        s = dashboard.snapshot([session(self.tmp)])["sessions"][0]
        self.assertEqual(s["halted"], "max drawdown")


class TestItOnlyEverReads(DashboardTestCase):
    def test_taking_a_snapshot_changes_nothing_on_disk(self):
        payload = write_state(self.tmp)
        payload["last_price"] = 60_000.0
        (self.tmp / "state.json").write_text(json.dumps(payload), encoding="utf-8")
        (self.tmp / "trades.csv").write_text(
            "n,opened,closed,days,side,qty,entry,exit,gross,costs,net,balance,reason\n",
            encoding="utf-8")

        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.tmp.iterdir()}
        for _ in range(3):
            dashboard.snapshot([session(self.tmp)])
        after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.tmp.iterdir()}
        self.assertEqual(before, after, "the dashboard wrote to a session's files")

    def test_it_creates_no_files_of_its_own(self):
        dashboard.snapshot([session(self.tmp)])
        self.assertEqual(list(self.tmp.iterdir()), [])


class TestItStaysOnThisMachine(unittest.TestCase):
    """A page showing positions and balances must not be reachable from the network."""

    def test_the_host_is_loopback(self):
        self.assertEqual(dashboard.HOST, "127.0.0.1")

    def test_the_server_binds_to_loopback_only(self):
        server = dashboard.serve([], port=0)
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
        finally:
            server.server_close()

    def test_the_page_and_the_api_both_answer(self):
        import urllib.request

        with tempfile.TemporaryDirectory() as tmp:
            server = dashboard.serve([session(Path(tmp))], port=0)
            port = server.server_address[1]
            import threading
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                page = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
                api = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state").read()
                self.assertIn("<title>tradebot</title>", page)
                self.assertIn("sessions", json.loads(api))
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()


class TestTheWaitIsExplained(DashboardTestCase):
    """"Waiting for its first bar" is the commonest thing a new user sees.

    On its own it says nothing about whether that is normal. It usually is: the shipped
    configs use hourly bars, so an empty dashboard for the best part of an hour is
    expected rather than broken, and the interval is what distinguishes the two.
    """

    def waiting(self, interval):
        s = dashboard.Session(
            name="buy_and_hold", state_file=self.tmp / "none.json",
            trades_file=self.tmp / "none.csv", starting_cash=1000.0, currency="£",
            symbol="BTC_USD", interval=interval,
        )
        return dashboard.snapshot([s])["sessions"][0]

    def test_it_reports_how_long_a_bar_takes(self):
        self.assertEqual(self.waiting("1h")["wait_minutes"], 60)
        self.assertEqual(self.waiting("1m")["wait_minutes"], 1)
        self.assertEqual(self.waiting("15m")["wait_minutes"], 15)

    def test_an_unknown_interval_gives_no_number_rather_than_a_wrong_one(self):
        self.assertIsNone(self.waiting("3s")["wait_minutes"])

    def test_the_symbol_and_interval_survive_having_no_state(self):
        """A session with nothing on disk still has to be able to describe itself."""
        s = self.waiting("1h")
        self.assertTrue(s["waiting"])
        self.assertEqual(s["symbol"], "BTC_USD")
        self.assertEqual(s["interval"], "1h")


class TestThePortfolioTotal(DashboardTestCase):
    """Several sessions are one allocation, not several experiments.

    Capital is split between them, so what matters is what the whole thing is worth.
    Watching four cards and doing the arithmetic by eye is how a portfolio that is down
    gets read as three that are fine and one that is not.
    """

    def sleeve(self, name, cash, qty, price, cash_per_sleeve=250.0, mark=True):
        payload = {
            "saved_at": 1, "started_at": 0, "symbol": name.upper(), "interval": "1h",
            "strategy": "vol_target", "live_bars": 10,
            "engine": {"portfolio": {"cash": cash, "qty": qty, "avg_price": price,
                                     "fees_paid": 1.0, "slippage_paid": 0.0}, "risk": {}},
        }
        if mark:
            payload["last_price"] = price
        (self.tmp / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
        return dashboard.Session(
            name=name, state_file=self.tmp / f"{name}.json",
            trades_file=self.tmp / "none.csv", starting_cash=cash_per_sleeve,
            currency="£", symbol=name.upper(), interval="1h",
        )

    def test_the_sleeves_are_added_up(self):
        sessions = [self.sleeve("btc", 100.0, 0.002, 80_000.0),
                    self.sleeve("eth", 50.0, 0.06, 3_500.0)]
        p = dashboard.snapshot(sessions)["portfolio"]
        self.assertEqual(p["sleeves"], 2)
        self.assertAlmostEqual(p["staked"], 500.0)
        self.assertAlmostEqual(p["equity"], 100 + 0.002 * 80_000 + 50 + 0.06 * 3_500)

    def test_the_return_is_measured_on_the_whole_allocation(self):
        sessions = [self.sleeve("btc", 300.0, 0.0, 80_000.0),
                    self.sleeve("eth", 200.0, 0.0, 3_500.0)]
        p = dashboard.snapshot(sessions)["portfolio"]
        self.assertAlmostEqual(p["equity"], 500.0)
        self.assertAlmostEqual(p["return_pct"], 0.0)

    def test_a_single_session_gets_no_portfolio_line(self):
        """One sleeve is not a portfolio; a total would just repeat the card above it."""
        self.assertIsNone(dashboard.snapshot([self.sleeve("btc", 250.0, 0.0, 80_000.0)])["portfolio"])

    def test_a_sleeve_that_cannot_be_valued_suppresses_the_total(self):
        """Summing the rest would silently understate the portfolio."""
        good = self.sleeve("btc", 100.0, 0.002, 80_000.0)
        blind = self.sleeve("eth", 50.0, 0.06, 3_500.0, mark=False)
        p = dashboard.snapshot([good, blind])["portfolio"]
        self.assertFalse(p["complete"])
        self.assertEqual(p["missing"], 1)
        self.assertIsNone(p["equity"], "showed a total that was missing a sleeve")
        self.assertIsNone(p["return_pct"])

    def test_a_sleeve_still_waiting_also_suppresses_the_total(self):
        good = self.sleeve("btc", 100.0, 0.002, 80_000.0)
        waiting = dashboard.Session(
            name="eth", state_file=self.tmp / "nothing.json",
            trades_file=self.tmp / "none.csv", starting_cash=250.0, currency="£",
            symbol="ETH_USD", interval="1h")
        p = dashboard.snapshot([good, waiting])["portfolio"]
        self.assertFalse(p["complete"])
        self.assertEqual(p["missing"], 1)

    def test_the_staked_total_counts_every_sleeve_even_the_blind_ones(self):
        """What was committed does not depend on what can currently be priced."""
        good = self.sleeve("btc", 100.0, 0.002, 80_000.0)
        blind = self.sleeve("eth", 50.0, 0.06, 3_500.0, mark=False)
        self.assertAlmostEqual(dashboard.snapshot([good, blind])["portfolio"]["staked"], 500.0)

    def test_it_names_the_markets_it_is_spread_across(self):
        sessions = [self.sleeve("btc", 100.0, 0.0, 80_000.0),
                    self.sleeve("eth", 100.0, 0.0, 3_500.0)]
        self.assertEqual(dashboard.snapshot(sessions)["portfolio"]["markets"], ["BTC", "ETH"])
