"""Tests for the stock study: the Yahoo feed, flat fees, CAGR and the benchmark."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tradebot import study as study_mod
from tradebot.backtest import run as run_backtest
from tradebot.costs import CostModel
from tradebot.engine import ExecutionSettings
from tradebot.feeds.synthetic import SyntheticFeed
from tradebot.feeds.yahoo import YahooError, YahooFeed
from tradebot.metrics import Metrics, summarise
from tradebot.risk import RiskLimits
from tradebot.strategies import build
from tradebot.types import EquityPoint

DAY_MS = 86_400_000


def yahoo_payload(closes, adjcloses=None, start=1_600_000_000, nulls=()):
    """Build a Yahoo chart response shaped like the real one."""
    n = len(closes)
    return {
        "chart": {
            "result": [
                {
                    "timestamp": [start + i * 86_400 for i in range(n)],
                    "indicators": {
                        "quote": [
                            {
                                "open": [None if i in nulls else c for i, c in enumerate(closes)],
                                "high": [None if i in nulls else c * 1.01 for i, c in enumerate(closes)],
                                "low": [None if i in nulls else c * 0.99 for i, c in enumerate(closes)],
                                "close": [None if i in nulls else c for i, c in enumerate(closes)],
                                "volume": [1000] * n,
                            }
                        ],
                        "adjclose": [{"adjclose": adjcloses or closes}],
                    },
                }
            ],
            "error": None,
        }
    }


class TestYahooParsing(unittest.TestCase):
    def test_parses_a_normal_response(self):
        feed = YahooFeed(symbol="TEST")
        candles = feed.parse(yahoo_payload([100.0, 101.0, 102.0]))
        self.assertEqual(len(candles), 3)
        self.assertAlmostEqual(candles[0].close, 100.0)
        self.assertEqual(candles[1].ts - candles[0].ts, DAY_MS)

    def test_dividend_adjustment_scales_the_whole_bar(self):
        """OHLC must be scaled by the same factor, or the bar's shape is corrupted."""
        feed = YahooFeed(symbol="TEST", adjust=True)
        candles = feed.parse(yahoo_payload([100.0], adjcloses=[90.0]))

        candle = candles[0]
        self.assertAlmostEqual(candle.close, 90.0, msg="close becomes the adjusted close")
        self.assertAlmostEqual(candle.high / candle.close, 1.01, places=6)
        self.assertAlmostEqual(candle.low / candle.close, 0.99, places=6)

    def test_adjustment_can_be_turned_off(self):
        feed = YahooFeed(symbol="TEST", adjust=False)
        candles = feed.parse(yahoo_payload([100.0], adjcloses=[90.0]))
        self.assertAlmostEqual(candles[0].close, 100.0)

    def test_null_bars_are_dropped_not_invented(self):
        """Yahoo pads holidays with nulls; a missing day is not a flat day."""
        feed = YahooFeed(symbol="TEST")
        candles = feed.parse(yahoo_payload([100.0, 101.0, 102.0, 103.0], nulls=(1, 2)))
        self.assertEqual(len(candles), 2)
        self.assertAlmostEqual(candles[0].close, 100.0)
        self.assertAlmostEqual(candles[1].close, 103.0)

    def test_an_all_null_series_raises(self):
        feed = YahooFeed(symbol="TEST")
        with self.assertRaises(YahooError):
            feed.parse(yahoo_payload([100.0, 101.0], nulls=(0, 1)))

    def test_an_empty_response_raises(self):
        feed = YahooFeed(symbol="TEST")
        with self.assertRaises(YahooError):
            feed.parse({"chart": {"result": [], "error": None}})

    def test_bad_arguments_are_rejected_early(self):
        with self.assertRaises(ValueError):
            YahooFeed(symbol="TEST", interval="7m")
        with self.assertRaises(ValueError):
            YahooFeed(symbol="TEST", range_="99y")


class TestFlatFees(unittest.TestCase):
    def test_a_flat_fee_is_added_to_every_fill(self):
        costs = CostModel(taker_fee_bps=0, half_spread_bps=0, slippage_bps=0, flat_fee=6.0)
        self.assertAlmostEqual(costs.fee(1000.0), 6.0)
        self.assertAlmostEqual(costs.fee(100_000.0), 6.0, msg="flat means flat")

    def test_breakeven_cash_counts_both_legs(self):
        costs = CostModel(taker_fee_bps=0, half_spread_bps=0, slippage_bps=0, flat_fee=6.0)
        self.assertAlmostEqual(costs.breakeven_cash(1000.0), 12.0)

    def test_a_flat_fee_is_brutal_on_small_positions(self):
        """The number that decides whether small-account trading can work at all."""
        costs = CostModel(taker_fee_bps=0, half_spread_bps=0, slippage_bps=0, flat_fee=6.0)
        self.assertAlmostEqual(costs.breakeven_move_pct(100.0), 12.0, msg="12% on a £100 trade")
        self.assertAlmostEqual(costs.breakeven_move_pct(10_000.0), 0.12)

    def test_breakeven_without_a_notional_reports_the_proportional_part(self):
        costs = CostModel(taker_fee_bps=10, half_spread_bps=2, slippage_bps=2, flat_fee=6.0)
        self.assertAlmostEqual(costs.breakeven_move_pct(), 0.28)

    def test_a_negative_flat_fee_is_rejected(self):
        with self.assertRaises(ValueError):
            CostModel(flat_fee=-1.0)


class TestCagr(unittest.TestCase):
    def _metrics(self, start, end, years) -> Metrics:
        curve = [
            EquityPoint(ts=0, equity=start, price=1.0, position=0.0, cash=start),
            EquityPoint(
                ts=int(years * 365 * DAY_MS), equity=end, price=1.0, position=0.0, cash=end
            ),
        ]
        return summarise(curve, [], starting_equity=start, fees_paid=0.0)

    def test_doubling_over_ten_years_is_about_seven_percent(self):
        self.assertAlmostEqual(self._metrics(1000.0, 2000.0, 10.0).cagr_pct, 7.18, places=1)

    def test_a_flat_decade_is_zero(self):
        self.assertAlmostEqual(self._metrics(1000.0, 1000.0, 10.0).cagr_pct, 0.0, places=6)

    def test_a_halving_is_negative(self):
        self.assertLess(self._metrics(1000.0, 500.0, 10.0).cagr_pct, 0.0)

    def test_a_wiped_out_account_does_not_explode(self):
        self.assertEqual(self._metrics(1000.0, 0.0, 10.0).cagr_pct, 0.0)


class TestBuyAndHold(unittest.TestCase):
    def setUp(self):
        self.candles = SyntheticFeed(bars=800, seed=3, drift_per_bar=0.0004).generate()
        self.limits = RiskLimits(
            max_position_pct=1.0, max_daily_loss_pct=0.99, max_drawdown_pct=0.99,
            max_trades_per_day=10_000, min_trade_notional=1.0, cooldown_bars_after_loss=0,
        )

    def _run(self, name, costs=None):
        return run_backtest(
            self.candles, build(name), starting_cash=10_000.0,
            costs=costs or CostModel(), limits=self.limits,
            execution=ExecutionSettings(min_notional=1.0),
        )

    def test_it_trades_exactly_twice(self):
        """In and out once. That is the whole point of the benchmark."""
        result = self._run("buy_and_hold")
        self.assertEqual(len(result.engine.portfolio.fills), 2)

    def test_it_pays_almost_nothing_in_costs(self):
        active = self._run("micro_scalp")
        passive = self._run("buy_and_hold")
        self.assertLess(passive.metrics.total_costs, active.metrics.total_costs / 10)

    def test_it_tracks_the_underlying_move(self):
        result = self._run("buy_and_hold", costs=CostModel(0, 0, 0, 0))
        market = self.candles[-1].close / self.candles[1].open - 1.0
        self.assertAlmostEqual(result.metrics.total_return_pct / 100.0, market, places=2)


class TestStudyReporting(unittest.TestCase):
    def _fake_metrics(self, total_return):
        curve = [
            EquityPoint(ts=0, equity=1000.0, price=1.0, position=0.0, cash=1000.0),
            EquityPoint(
                ts=10 * 365 * DAY_MS,
                equity=1000.0 * (1 + total_return / 100),
                price=1.0, position=0.0, cash=0.0,
            ),
        ]
        return summarise(curve, [], starting_equity=1000.0, fees_paid=0.0)

    def _study(self, benchmark_return, strategy_return):
        study = study_mod.Study()
        study.rows = [
            study_mod.Row("SPY", "buy_and_hold", self._fake_metrics(benchmark_return)),
            study_mod.Row("SPY", "ema_cross", self._fake_metrics(strategy_return)),
        ]
        study.spans = {"SPY": "test span"}
        return study

    def test_a_losing_strategy_is_reported_as_losing(self):
        text = study_mod.render(self._study(200.0, 50.0))
        self.assertIn("0 of 1 strategy runs beat", text)
        self.assertIn("-150.0pp", text)

    def test_a_winning_strategy_is_flagged_but_hedged(self):
        text = study_mod.render(self._study(50.0, 200.0))
        self.assertIn("1 of 1 strategy runs beat", text)
        self.assertIn("luck", text.lower())

    def test_the_benchmark_is_added_even_if_not_requested(self):
        study = study_mod.Study()
        study.rows = [study_mod.Row("SPY", "ema_cross", self._fake_metrics(10.0))]
        self.assertIsNone(study.benchmark_for("SPY"))
        self.assertEqual(study.strategies(), ["ema_cross"])


if __name__ == "__main__":
    unittest.main()


class TestTradeLog(unittest.TestCase):
    """The trade log is how results get checked, so its arithmetic must tie out."""

    def _trades(self):
        from tradebot.types import Side, Trade
        return [
            Trade(entry_ts=0, exit_ts=DAY_MS * 5, side=Side.BUY, qty=10.0,
                  entry_price=100.0, exit_price=110.0, fees=2.0,
                  entry_reference=100.0, exit_reference=110.0, reason="take profit"),
            Trade(entry_ts=DAY_MS * 6, exit_ts=DAY_MS * 8, side=Side.BUY, qty=5.0,
                  entry_price=110.0, exit_price=105.0, fees=1.0,
                  entry_reference=110.0, exit_reference=105.0, reason="stop loss"),
        ]

    def test_the_running_balance_accumulates_net_pnl(self):
        from tradebot import tradelog
        rows = tradelog.rows(self._trades(), starting_cash=1000.0)
        self.assertAlmostEqual(rows[0]["balance"], 1000.0 + 98.0)
        self.assertAlmostEqual(rows[1]["balance"], 1000.0 + 98.0 - 26.0)

    def test_days_held_is_reported(self):
        from tradebot import tradelog
        rows = tradelog.rows(self._trades())
        self.assertAlmostEqual(rows[0]["days"], 5.0)
        self.assertAlmostEqual(rows[1]["days"], 2.0)

    def test_the_table_renders_without_colour_codes_when_asked(self):
        from tradebot import tradelog
        text = tradelog.render(self._trades(), starting_cash=1000.0, colour=False)
        self.assertNotIn("\033", text)
        self.assertIn("take profit", text)
        self.assertIn("2 trades: 1 winners, 1 losers", text)

    def test_an_empty_log_says_so_rather_than_crashing(self):
        from tradebot import tradelog
        self.assertIn("No trades", tradelog.render([], starting_cash=1000.0))

    def test_csv_export_round_trips(self):
        import csv as csvmod
        from tradebot import tradelog
        path = Path(tempfile.mkdtemp()) / "trades.csv"
        tradelog.write_csv(path, self._trades(), starting_cash=1000.0)
        with path.open(encoding="utf-8") as handle:
            rows = list(csvmod.DictReader(handle))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["reason"], "take profit")
        self.assertAlmostEqual(float(rows[1]["balance"]), 1072.0)

    def test_append_writes_one_header_and_grows(self):
        import csv as csvmod
        from tradebot import tradelog
        path = Path(tempfile.mkdtemp()) / "live.csv"
        for i, trade in enumerate(self._trades()):
            tradelog.append(path, trade, balance=1000.0 + i)
        with path.open(encoding="utf-8") as handle:
            rows = list(csvmod.DictReader(handle))
        self.assertEqual(len(rows), 2, "appending must not rewrite or duplicate the header")

    def test_appended_trades_keep_counting_up(self):
        """A resumed run must not restart numbering at 1 on every append."""
        import csv as csvmod
        from tradebot import tradelog
        path = Path(tempfile.mkdtemp()) / "live.csv"
        for trade in self._trades():
            tradelog.append(path, trade, balance=1000.0)
        for trade in self._trades():
            tradelog.append(path, trade, balance=1000.0)
        with path.open(encoding="utf-8") as handle:
            rows = list(csvmod.DictReader(handle))
        self.assertEqual([r["n"] for r in rows], ["1", "2", "3", "4"])

    def test_the_limit_note_reports_how_many_were_hidden(self):
        from tradebot import tradelog
        text = tradelog.render(self._trades(), limit=1, colour=False)
        self.assertIn("and 1 more", text)


class TestShortRunsAreReportedHonestly(unittest.TestCase):
    """A number that is suppressed must not be printed as if it were measured.

    On a run too short to annualise, CAGR is set to zero - and rendering that as
    "0.00%" says the strategy made nothing, when the case that produces it is often a
    run that made a great deal in a few days. That is the exact failure mode this
    package spends a README complaining about, printed in its own output. The same
    goes for a Sharpe ratio annualised up from fifty bars, and for a profit factor of
    infinity, which reads as a triumph rather than as "nothing has lost money yet".
    """

    def short_run(self, bars=50, growth=1.15):
        from tradebot.strategies import build
        from tradebot.types import Candle

        candles = [
            Candle(ts=1_700_000_000_000 + i * 60_000, open=p, high=p * 1.01,
                   low=p * 0.99, close=p, volume=1.0)
            for i, p in enumerate(100.0 * growth**i for i in range(bars))
        ]
        return run_backtest(
            candles, build("buy_and_hold"), starting_cash=1000.0,
            limits=RiskLimits(max_position_pct=1.0, max_daily_loss_pct=0.99,
                              max_drawdown_pct=0.99, max_trades_per_day=1000,
                              min_trade_notional=1.0, cooldown_bars_after_loss=0),
            execution=ExecutionSettings(min_notional=1.0),
        ).metrics

    def test_a_short_run_does_not_report_a_cagr_at_all(self):
        metrics = self.short_run()
        self.assertFalse(metrics.can_annualise)
        rendered = metrics.render()
        self.assertIn("Annualised (CAGR)", rendered)
        self.assertIn("too short to annualise", rendered)
        self.assertNotIn("Annualised (CAGR)            0.00%", rendered)

    def test_the_run_that_triggers_it_actually_made_money(self):
        """Guards against the fix hiding a real zero instead of a suppressed one."""
        self.assertGreater(self.short_run().total_return_pct, 100.0)

    def test_a_short_run_does_not_report_a_sharpe_ratio(self):
        rendered = self.short_run().render()
        self.assertIn("Sharpe (annualised)", rendered)
        self.assertRegex(rendered, r"Sharpe \(annualised\)\s+n/a")

    def test_an_infinite_profit_factor_is_explained_not_printed(self):
        rendered = self.short_run().render()
        self.assertNotIn("inf", rendered)
        self.assertIn("nothing has lost money yet", rendered)

    def test_the_span_is_given_in_a_unit_that_reads_sensibly(self):
        self.assertIn("minutes", self.short_run(bars=50).render())

    def test_a_long_run_still_reports_both_figures(self):
        from tradebot.strategies import build

        candles = SyntheticFeed(bars=3000, seed=4, interval_ms=86_400_000).generate()
        metrics = run_backtest(
            candles, build("buy_and_hold"), starting_cash=1000.0,
            limits=RiskLimits(max_position_pct=1.0, max_daily_loss_pct=0.99,
                              max_drawdown_pct=0.99, max_trades_per_day=1000,
                              min_trade_notional=1.0, cooldown_bars_after_loss=0),
            execution=ExecutionSettings(min_notional=1.0),
        ).metrics
        self.assertTrue(metrics.can_annualise)
        self.assertNotIn("too short to annualise", metrics.render())

    def test_cost_drag_is_still_annualised_on_a_short_run(self):
        """Deliberately exempt: it scales linearly, and it has to warn early to help."""
        metrics = self.short_run()
        self.assertFalse(metrics.can_annualise)
        self.assertGreater(metrics.cost_drag_annual_pct, 0.0)
        self.assertIn("%/year at this rate", metrics.render())


class TestSpanPhrasing(unittest.TestCase):
    def test_it_reads_correctly_at_every_scale(self):
        from tradebot.metrics import _too_short

        minute, hour, day = 1 / 365 / 24 / 60, 1 / 365 / 24, 1 / 365
        self.assertIn("1 minute -", _too_short(minute))
        self.assertIn("2 minutes", _too_short(2 * minute))
        self.assertIn("1.0 hour -", _too_short(hour))
        self.assertIn("5.0 hours", _too_short(5 * hour))
        self.assertIn("1.0 day -", _too_short(day))
        self.assertIn("2.1 days", _too_short(2.1 * day))
