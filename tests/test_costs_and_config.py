"""Cost model, config loading and CSV parsing."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tradebot import config as config_mod
from tradebot.costs import CostModel
from tradebot.feeds.csv_feed import CsvFeed, parse_timestamp, write_csv
from tradebot.types import Candle, Liquidity, Side


class TestCostModel(unittest.TestCase):
    def test_buys_fill_above_the_reference_and_sells_below(self):
        costs = CostModel(half_spread_bps=10, slippage_bps=10)
        self.assertGreater(costs.fill_price(Side.BUY, 100.0), 100.0)
        self.assertLess(costs.fill_price(Side.SELL, 100.0), 100.0)

    def test_adverse_movement_is_symmetric(self):
        costs = CostModel(half_spread_bps=10, slippage_bps=10)
        buy = costs.fill_price(Side.BUY, 100.0) - 100.0
        sell = 100.0 - costs.fill_price(Side.SELL, 100.0)
        self.assertAlmostEqual(buy, sell)

    def test_round_trip_is_twice_the_one_way_cost(self):
        costs = CostModel(taker_fee_bps=10, half_spread_bps=2, slippage_bps=2)
        self.assertAlmostEqual(costs.round_trip_bps, 28.0)
        self.assertAlmostEqual(costs.breakeven_move_pct(), 0.28)

    def test_breakeven_cash_scales_with_position_size(self):
        costs = CostModel(taker_fee_bps=10, half_spread_bps=2, slippage_bps=2)
        self.assertAlmostEqual(costs.breakeven_cash(1000.0), 2.80)
        self.assertAlmostEqual(costs.breakeven_cash(10_000.0), 28.00)

    def test_a_fixed_target_has_a_break_even_position_size(self):
        """The precise version of this project's premise.

        A fixed cash target is not doomed at *every* size - it is doomed above a
        threshold, because cost scales with position while the target does not. For a
        5p target on the default costs the threshold is about £18. Below it the trade
        clears costs but earns pennies; above it, every win is a net loss.
        """
        costs = CostModel()
        target_profit = 0.05
        breakeven_size = target_profit / (costs.round_trip_bps * 1e-4)

        self.assertAlmostEqual(breakeven_size, 17.857, places=2)
        self.assertLess(costs.breakeven_cash(breakeven_size * 0.5), target_profit)
        for size in (100, 1_000, 10_000, 100_000):
            self.assertGreater(
                costs.breakeven_cash(size), target_profit,
                msg=f"a 5p target must lose money on a {size} position",
            )

    def test_a_tiny_percentage_target_loses_at_any_size(self):
        """A target expressed as a *percentage* below the round trip never works.

        This is the case with no escape hatch: both sides scale together, so position
        size cancels out entirely.
        """
        costs = CostModel()
        target_pct = 0.0005  # what micro_scalp asks for: 0.05%
        for size in (10, 100, 1_000, 100_000):
            self.assertGreater(costs.breakeven_cash(size), size * target_pct)

    def test_maker_orders_are_cheaper_than_taker(self):
        costs = CostModel(taker_fee_bps=10, maker_fee_bps=2)
        self.assertLess(
            costs.fee(1000.0, Liquidity.MAKER),
            costs.fee(1000.0, Liquidity.TAKER),
        )

    def test_negative_costs_are_rejected(self):
        with self.assertRaises(ValueError):
            CostModel(taker_fee_bps=-1)


class TestConfig(unittest.TestCase):
    def _write(self, text):
        path = Path(tempfile.mkdtemp()) / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_defaults_load_when_no_file_exists(self):
        config = config_mod.load(None)
        self.assertEqual(config.market.symbol, "BTC_USD")
        self.assertFalse(config.live.enabled, "live trading must default to off")
        self.assertTrue(config.live.dry_run, "dry run must default to on")

    def test_values_are_read_from_the_file(self):
        path = self._write(
            """
            [market]
            symbol = "ETH_USD"
            interval = "1h"

            [costs]
            taker_fee_bps = 25.0

            [risk]
            max_position_pct = 0.1
            """
        )
        config = config_mod.load(path)
        self.assertEqual(config.market.symbol, "ETH_USD")
        self.assertAlmostEqual(config.costs.taker_fee_bps, 25.0)
        self.assertAlmostEqual(config.risk.max_position_pct, 0.1)

    def test_an_unknown_setting_is_an_error_not_a_shrug(self):
        """A silently ignored risk limit is how people lose money to a typo."""
        path = self._write("[risk]\nmax_postion_pct = 0.1\n")
        with self.assertRaises(ValueError) as caught:
            config_mod.load(path)
        self.assertIn("max_postion_pct", str(caught.exception))

    def test_invalid_limits_raise_on_load(self):
        path = self._write("[risk]\nmax_drawdown_pct = 5.0\n")
        with self.assertRaises(ValueError):
            config_mod.load(path)

    def test_strategy_params_are_kept_as_a_dict(self):
        path = self._write('[strategy]\nname = "ema_cross"\n\n[strategy.params]\nfast = 5\nslow = 20\n')
        config = config_mod.load(path)
        self.assertEqual(config.strategy.params, {"fast": 5, "slow": 20})

    def test_armed_live_trading_produces_a_warning(self):
        path = self._write("[live]\nenabled = true\ndry_run = false\n")
        config = config_mod.load(path)
        self.assertTrue(any("LIVE TRADING IS ARMED" in w for w in config.validate()))

    def test_missing_explicit_config_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            config_mod.load("/nonexistent/config.toml")


class TestCsvFeed(unittest.TestCase):
    def test_timestamp_formats(self):
        self.assertEqual(parse_timestamp("1700000000"), 1_700_000_000_000)
        self.assertEqual(parse_timestamp("1700000000000"), 1_700_000_000_000)
        self.assertEqual(parse_timestamp("2023-11-14T22:13:20Z"), 1_700_000_000_000)

    def test_round_trips_through_write_and_read(self):
        candles = [
            Candle(ts=1_700_000_000_000, open=100, high=110, low=90, close=105, volume=1.0),
            Candle(ts=1_700_000_060_000, open=105, high=115, low=95, close=110, volume=2.0),
        ]
        path = Path(tempfile.mkdtemp()) / "history.csv"
        write_csv(path, candles)
        loaded = CsvFeed(path).load()

        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0].ts, candles[0].ts)
        self.assertAlmostEqual(loaded[1].close, 110.0)

    def test_out_of_order_rows_are_sorted(self):
        path = Path(tempfile.mkdtemp()) / "unsorted.csv"
        path.write_text(
            "ts,open,high,low,close\n"
            "200,2,2,2,2\n"
            "100,1,1,1,1\n",
            encoding="utf-8",
        )
        loaded = CsvFeed(path).load()
        self.assertLess(loaded[0].ts, loaded[1].ts)

    def test_a_close_only_file_is_accepted(self):
        path = Path(tempfile.mkdtemp()) / "closes.csv"
        path.write_text("time,price\n100,50\n200,55\n", encoding="utf-8")
        loaded = CsvFeed(path).load()
        self.assertAlmostEqual(loaded[0].high, 50.0, msg="OHLC falls back to the close")

    def test_a_bad_row_names_its_line_number(self):
        path = Path(tempfile.mkdtemp()) / "broken.csv"
        path.write_text("ts,close\n100,50\n200,not-a-number\n", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            CsvFeed(path).load()
        self.assertIn(":3", str(caught.exception))


class TestCandleValidation(unittest.TestCase):
    def test_high_below_low_is_rejected(self):
        with self.assertRaises(ValueError):
            Candle(ts=1, open=10, high=5, low=8, close=9, volume=1)

    def test_a_negative_price_is_rejected(self):
        with self.assertRaises(ValueError):
            Candle(ts=1, open=-1, high=10, low=1, close=5, volume=1)


class TestLotSizeAlignment(unittest.TestCase):
    """The simulator must round quantities the way the venue will.

    ``execution.qty_step`` and ``live.qty_decimals`` describe the same physical fact
    from opposite ends, and their defaults disagreed: the backtester rounded to
    nothing while the live broker floors to six decimals. Set a coarse precision for
    your venue and the backtest filled sizes the exchange would truncate - the exact
    paper-versus-live divergence the shared engine exists to prevent.
    """

    def write(self, body):
        handle = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        handle.write(body)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_qty_step_follows_the_venue_precision_by_default(self):
        path = self.write("[live]\nqty_decimals = 2\n")
        self.assertAlmostEqual(config_mod.load(path).execution.qty_step, 0.01)

    def test_the_defaults_agree_with_each_other(self):
        loaded = config_mod.load(self.write(""))
        self.assertAlmostEqual(loaded.execution.qty_step, 10.0**-loaded.live.qty_decimals)

    def test_an_explicit_lot_size_is_never_overridden(self):
        """A venue step of 0.25 cannot be written as a number of decimals at all."""
        path = self.write("[execution]\nqty_step = 0.25\n\n[live]\nqty_decimals = 6\n")
        self.assertAlmostEqual(config_mod.load(path).execution.qty_step, 0.25)

    def test_zero_decimals_means_whole_units(self):
        path = self.write("[live]\nqty_decimals = 0\n")
        self.assertAlmostEqual(config_mod.load(path).execution.qty_step, 1.0)

    def test_the_engine_and_the_venue_round_a_size_to_the_same_place(self):
        """The point of the alignment: both paths must truncate identically."""
        from tradebot.brokers.cryptocom import floor_to_decimals

        for decimals in range(0, 7):
            with self.subTest(decimals=decimals):
                loaded = config_mod.load(self.write(f"[live]\nqty_decimals = {decimals}\n"))
                for qty in (0.123456789, 1.9999999, 12.5, 0.000001):
                    self.assertAlmostEqual(
                        loaded.execution.round_qty(qty),
                        floor_to_decimals(qty, decimals),
                        places=9,
                        msg=f"simulator and venue disagree on {qty} at {decimals} dp",
                    )


if __name__ == "__main__":
    unittest.main()


class TestPathsExpandWithoutAShell(unittest.TestCase):
    """Globs must be expanded by the program, not by the shell.

    Unix shells turn `configs/*.toml` into a list before the program starts. The Windows
    command prompt hands the asterisk over untouched, so a command copied straight from
    the README worked on one machine and died on another with a stack trace about a file
    literally named `*.toml`. Expanding here makes one documented command line behave
    the same everywhere.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name in ("alpha.toml", "beta.toml", "notes.txt"):
            (self.tmp / name).write_text("", encoding="utf-8")

    def test_an_unexpanded_glob_is_expanded(self):
        from tradebot.cli import expand_paths

        found = expand_paths([str(self.tmp / "*.toml")])
        self.assertEqual([Path(p).name for p in found], ["alpha.toml", "beta.toml"])

    def test_already_expanded_arguments_pass_straight_through(self):
        """A Unix shell has already done the work; doing it twice must be harmless."""
        from tradebot.cli import expand_paths

        given = [str(self.tmp / "alpha.toml"), str(self.tmp / "beta.toml")]
        self.assertEqual(expand_paths(given), given)

    def test_duplicates_are_collapsed(self):
        from tradebot.cli import expand_paths

        one = str(self.tmp / "alpha.toml")
        self.assertEqual(expand_paths([one, one, str(self.tmp / "*.toml")]),
                         [one, str(self.tmp / "beta.toml")])

    def test_directories_are_not_mistaken_for_files(self):
        from tradebot.cli import expand_paths

        (self.tmp / "sub.toml").mkdir()
        found = expand_paths([str(self.tmp / "*.toml")])
        self.assertNotIn("sub.toml", [Path(p).name for p in found])

    def test_matching_nothing_says_so_instead_of_crashing(self):
        from tradebot.cli import expand_paths

        with self.assertRaises(SystemExit) as caught:
            expand_paths([str(self.tmp / "*.nope")])
        self.assertIn("no file matched", str(caught.exception))

    def test_the_message_points_at_the_usual_cause(self):
        """Being in the wrong directory is what this is nearly always telling you."""
        from tradebot.cli import expand_paths

        with self.assertRaises(SystemExit) as caught:
            expand_paths([str(self.tmp / "no-such-place" / "*.toml")], "config")
        message = str(caught.exception)
        self.assertIn("no config matched", message)
        self.assertIn("project folder", message)
