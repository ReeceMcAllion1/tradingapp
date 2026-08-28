"""Tests for the stock study: the Yahoo feed, flat fees, CAGR and the benchmark."""

from __future__ import annotations

import unittest

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
