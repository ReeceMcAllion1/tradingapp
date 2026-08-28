"""Golden-value regression test for the whole engine.

Every other test checks one behaviour in isolation. This one pins the end-to-end
result: six strategies over the same 3,000 deterministic bars, at fixed costs and
limits, down to the penny.

Its job is to catch the change nobody meant to make. A tweak to fill pricing, fee
attribution, bracket resolution, position sizing or the risk manager will move these
numbers, and a silent shift in any of them would invalidate every result in the README
while every isolated unit test kept passing. That is exactly how a backtester rots.

If a change here is deliberate, verify it is what you intended and update the table -
but never update it just to make the suite green.
"""

from __future__ import annotations

import unittest

from tradebot import backtest
from tradebot.costs import CostModel
from tradebot.engine import ExecutionSettings
from tradebot.feeds.synthetic import SyntheticFeed
from tradebot.risk import RiskLimits
from tradebot.strategies import available, build

#: strategy -> (ending equity, trades, total costs), on seed 1234 over 3,000 bars.
GOLDEN = {
    "buy_and_hold": (10748.508376, 1, 21.786605),
    "ema_cross": (8437.317917, 72, 1378.924092),
    "mean_reversion": (9364.598766, 49, 1011.719439),
    "micro_scalp": (3422.909976, 545, 7156.845706),
    "never_lose": (9451.569847, 71, 1491.235733),
    "slow_trend": (10492.556940, 2, 43.509538),
}


class TestGoldenResults(unittest.TestCase):
    def setUp(self):
        self.candles = SyntheticFeed(bars=3000, seed=1234).generate()
        self.kwargs = dict(
            starting_cash=10_000.0,
            costs=CostModel(taker_fee_bps=7.5, maker_fee_bps=7.5,
                            half_spread_bps=1, slippage_bps=2),
            limits=RiskLimits(max_position_pct=1.0, max_daily_loss_pct=0.99,
                              max_drawdown_pct=0.99, max_trades_per_day=10_000,
                              min_trade_notional=1.0, cooldown_bars_after_loss=0),
            execution=ExecutionSettings(min_notional=1.0),
        )

    def test_every_strategy_reproduces_its_recorded_result(self):
        for name, (equity, trades, costs) in GOLDEN.items():
            with self.subTest(strategy=name):
                m = backtest.run(self.candles, build(name), **self.kwargs).metrics
                self.assertAlmostEqual(m.ending_equity, equity, places=4)
                self.assertEqual(m.trades, trades)
                self.assertAlmostEqual(m.total_costs, costs, places=4)

    def test_the_table_covers_every_registered_strategy(self):
        """A new strategy must be pinned too, or it silently escapes this net."""
        self.assertEqual(set(GOLDEN), set(available()))

    def test_the_input_data_is_itself_deterministic(self):
        """If the fixture drifts, the golden values mean nothing."""
        again = SyntheticFeed(bars=3000, seed=1234).generate()
        self.assertEqual(len(again), len(self.candles))
        self.assertAlmostEqual(again[0].open, self.candles[0].open, places=10)
        self.assertAlmostEqual(again[-1].close, self.candles[-1].close, places=10)

    def test_results_are_reproducible_within_a_run(self):
        first = backtest.run(self.candles, build("ema_cross"), **self.kwargs).metrics
        second = backtest.run(self.candles, build("ema_cross"), **self.kwargs).metrics
        self.assertAlmostEqual(first.ending_equity, second.ending_equity, places=10)


if __name__ == "__main__":
    unittest.main()
