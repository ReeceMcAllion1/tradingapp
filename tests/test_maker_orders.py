"""Resting limit orders, and the reason the cheaper fee is not free money.

Venues charge makers less than takers - half, on this package's defaults - so routing
orders passively is the one cost saving available that needs no forecast at all. It is
also the easiest place in this codebase to lie to yourself, because the saving is
visible in the fee column while the thing it costs you is not.

A resting buy fills only when price comes down to it. When price runs away upward you
do not fill, and you miss the move you wanted to catch. So you are systematically
filled on the trades that immediately go against you and left behind on the ones that
would have worked. Model the discount without the miss and maker orders look like free
money in every backtest ever run.

These tests pin the mechanism and, crucially, the pessimism dial: a measurement that
only holds under the kindest fill assumption is not a result.
"""

from __future__ import annotations

import unittest

from tradebot.costs import CostModel
from tradebot.engine import Engine, ExecutionSettings
from tradebot.portfolio import Portfolio
from tradebot.risk import RiskLimits, RiskManager
from tradebot.strategies.base import Strategy
from tradebot.types import Candle, Decision, Liquidity

MINUTE = 60_000


def candle(i, open_, high, low, close):
    return Candle(ts=i * MINUTE, open=open_, high=high, low=low, close=close, volume=1.0)


class Scripted(Strategy):
    name = "scripted"

    def __init__(self, decisions):
        self.decisions = list(decisions)

    def on_candle(self, c, ctx):
        return self.decisions.pop(0) if self.decisions else Decision(None, reason="hold")


def engine(strategy, cash=10_000.0, costs=None, **ex):
    costs = costs or CostModel(taker_fee_bps=10, maker_fee_bps=5, half_spread_bps=1, slippage_bps=2)
    limits = RiskLimits(max_position_pct=1.0, max_daily_loss_pct=0.99, max_drawdown_pct=0.99,
                        max_trades_per_day=10_000, min_trade_notional=1.0, cooldown_bars_after_loss=0)
    pf = Portfolio(starting_cash=cash)
    eng = Engine(strategy=strategy, portfolio=pf, risk=RiskManager(limits=limits, costs=costs),
                 costs=costs, execution=ExecutionSettings(min_notional=1.0, **ex))
    return eng, pf


class TestItRestsRatherThanCrossing(unittest.TestCase):
    def test_a_buy_fills_at_the_limit_when_price_comes_down_to_it(self):
        eng, pf = engine(Scripted([Decision(1.0, reason="go")]), maker_offset_bps=50)
        eng.process(candle(1, 100, 100, 100, 100))
        # limit sits at 99.50; this bar trades down to 99.00, so it fills
        eng.process(candle(2, 100, 101, 99, 100))
        self.assertGreater(pf.qty, 0)
        self.assertEqual(pf.fills[-1].liquidity, Liquidity.MAKER)
        self.assertAlmostEqual(pf.fills[-1].price, 99.5, places=6)
        self.assertEqual(eng.maker_fills, 1)

    def test_a_maker_fill_pays_the_maker_fee_not_the_taker_one(self):
        costs = CostModel(taker_fee_bps=10, maker_fee_bps=5, half_spread_bps=0, slippage_bps=0)
        eng, pf = engine(Scripted([Decision(1.0, reason="go")]), costs=costs, maker_offset_bps=50)
        eng.process(candle(1, 100, 100, 100, 100))
        eng.process(candle(2, 100, 101, 99, 100))
        fill = pf.fills[-1]
        self.assertAlmostEqual(fill.fee, fill.qty * fill.price * 5e-4, places=6)

    def test_a_maker_fill_does_not_pay_the_spread(self):
        """It never crossed the book, so charging it to cross would be fiction."""
        costs = CostModel(taker_fee_bps=0, maker_fee_bps=0, half_spread_bps=20, slippage_bps=20)
        eng, pf = engine(Scripted([Decision(1.0, reason="go")]), costs=costs, maker_offset_bps=50)
        eng.process(candle(1, 100, 100, 100, 100))
        eng.process(candle(2, 100, 101, 99, 100))
        self.assertAlmostEqual(pf.fills[-1].price, 99.5, places=6)

    def test_a_gap_through_a_resting_limit_fills_better_not_worse(self):
        """A book cannot fill you worse than the price you named.

        Only reachable while an order rests across bars: the limit is priced off the
        open of the bar it is placed on, so it can never start below that bar's open.
        A later bar can gap under it, and then the open is the better price.
        """
        eng, pf = engine(Scripted([Decision(1.0, reason="go")]),
                         maker_offset_bps=50, maker_max_wait_bars=4)
        eng.process(candle(1, 100, 100, 100, 100))
        eng.process(candle(2, 100, 108, 100, 107))   # limit set at 99.50, not reached
        self.assertIsNotNone(eng.resting)
        eng.process(candle(3, 95, 96, 94, 95))       # gapped under it overnight
        self.assertAlmostEqual(pf.fills[-1].price, 95.0, places=6)


class TestTheMissIsTheCost(unittest.TestCase):
    """The half everyone forgets: what happens when price runs away."""

    def test_price_running_away_leaves_you_unfilled(self):
        eng, pf = engine(Scripted([Decision(1.0, reason="go")]),
                         maker_offset_bps=50, maker_then_take=False)
        eng.process(candle(1, 100, 100, 100, 100))
        eng.process(candle(2, 100, 108, 100, 107))   # never traded down to 99.50
        self.assertTrue(pf.is_flat, "filled on a bar that never reached the limit")
        self.assertEqual(eng.maker_misses, 1)

    def test_an_abandoned_order_costs_nothing_and_holds_nothing(self):
        eng, pf = engine(Scripted([Decision(1.0, reason="go")]),
                         maker_offset_bps=50, maker_then_take=False)
        eng.process(candle(1, 100, 100, 100, 100))
        eng.process(candle(2, 100, 108, 100, 107))
        self.assertEqual(len(pf.fills), 0)
        self.assertAlmostEqual(pf.cash, 10_000.0)

    def test_crossing_after_the_wait_pays_the_taker_fee(self):
        eng, pf = engine(Scripted([Decision(1.0, reason="go")]),
                         maker_offset_bps=50, maker_then_take=True)
        eng.process(candle(1, 100, 100, 100, 100))
        eng.process(candle(2, 100, 108, 100, 107))
        self.assertFalse(pf.is_flat, "the fallback should have taken the position")
        self.assertEqual(pf.fills[-1].liquidity, Liquidity.TAKER)
        self.assertEqual(eng.maker_misses, 1)

    def test_crossing_late_buys_at_a_worse_price_than_the_limit(self):
        """Missing and then chasing is the adverse selection, priced."""
        eng, pf = engine(Scripted([Decision(1.0, reason="go")]), maker_offset_bps=50)
        eng.process(candle(1, 100, 100, 100, 100))
        eng.process(candle(2, 100, 108, 100, 107))
        self.assertGreater(pf.fills[-1].price, 99.5,
                           "the chase must cost more than the limit that was missed")

    def test_it_waits_the_configured_number_of_bars(self):
        eng, pf = engine(Scripted([Decision(1.0, reason="go")]),
                         maker_offset_bps=50, maker_max_wait_bars=3, maker_then_take=False)
        eng.process(candle(1, 100, 100, 100, 100))
        for i in (2, 3):
            eng.process(candle(i, 100, 108, 100, 107))
            self.assertIsNotNone(eng.resting, f"gave up on bar {i}")
        eng.process(candle(4, 100, 108, 100, 107))
        self.assertIsNone(eng.resting)


class TestTheQueueDial(unittest.TestCase):
    """A result that only holds when you assume you fill easily is not a result."""

    def test_touching_the_limit_fills_when_no_queue_is_modelled(self):
        eng, pf = engine(Scripted([Decision(1.0, reason="go")]),
                         maker_offset_bps=50, maker_queue_bps=0)
        eng.process(candle(1, 100, 100, 100, 100))
        eng.process(candle(2, 100, 101, 99.5, 100))   # exactly touches
        self.assertFalse(pf.is_flat)

    def test_merely_touching_does_not_fill_when_a_queue_is_modelled(self):
        """Others were resting at that price first; the level bounced and they filled."""
        eng, pf = engine(Scripted([Decision(1.0, reason="go")]),
                         maker_offset_bps=50, maker_queue_bps=20, maker_then_take=False)
        eng.process(candle(1, 100, 100, 100, 100))
        eng.process(candle(2, 100, 101, 99.5, 100))
        self.assertTrue(pf.is_flat, "filled from the back of the queue on a single touch")
        self.assertEqual(eng.maker_misses, 1)

    def test_trading_well_through_the_limit_fills_even_with_a_queue(self):
        eng, pf = engine(Scripted([Decision(1.0, reason="go")]),
                         maker_offset_bps=50, maker_queue_bps=20)
        eng.process(candle(1, 100, 100, 100, 100))
        eng.process(candle(2, 100, 101, 98, 100))
        self.assertFalse(pf.is_flat)
        self.assertEqual(eng.maker_fills, 1)

    def test_a_harsher_queue_fills_less_often(self):
        """Alternating in and out of a market whose dips only just reach the limit."""
        def fills(queue):
            eng, _ = engine(Scripted([Decision(1.0 if i % 2 == 0 else 0.0) for i in range(60)]),
                            maker_offset_bps=20, maker_queue_bps=queue, maker_then_take=False)
            for i in range(60):
                # dips reach ~25bp below the open: past a 20bp limit, but not far past
                eng.process(candle(i, 100.0, 100.30, 99.75, 100.0))
            return eng.maker_fills

        easy, hard = fills(0), fills(40)
        self.assertGreater(easy, 0, "the fixture never fills even with no queue")
        self.assertGreater(easy, hard, f"queue dial did nothing: {easy} vs {hard}")


class TestGettingOutIsNeverPassive(unittest.TestCase):
    """A limit exit in a crash is an order that does not fill exactly when it must."""

    def test_a_stop_loss_cancels_any_resting_order(self):
        eng, pf = engine(Scripted([Decision(1.0, reason="go")]), maker_offset_bps=50)
        eng.process(candle(1, 100, 100, 100, 100))
        eng.process(candle(2, 100, 101, 99, 100))     # maker fill establishes the position
        eng.pending = Decision(1.0, stop_loss=95.0, reason="add more")
        eng.process(candle(3, 94, 94, 90, 91))        # stop triggers
        self.assertIsNone(eng.resting, "a resting order survived a stop-loss exit")
        self.assertTrue(pf.is_flat)

    def test_an_unfilled_order_is_cancelled_by_a_stop(self):
        """The dangerous case: a buy still queued while the position is being stopped out."""
        eng, pf = engine(Scripted([Decision(1.0, stop_loss=95.0, reason="go")]),
                         maker_offset_bps=50, maker_max_wait_bars=5)
        # (the stop now survives the hold bars in between - see TestHoldingKeepsItsProtection)
        eng.process(candle(1, 100, 100, 100, 100))
        eng.process(candle(2, 100, 101, 99, 100))     # position established
        eng.pending = Decision(1.0, stop_loss=95.0, reason="add more")
        eng.process(candle(3, 100, 108, 100, 107))    # add rests, never fills
        self.assertIsNotNone(eng.resting)
        eng.process(candle(4, 94, 94, 90, 91))        # stop fires
        self.assertIsNone(eng.resting, "a queued buy outlived the stop that flattened us")
        self.assertTrue(pf.is_flat)

    def test_closing_a_position_crosses_the_spread(self):
        eng, pf = engine(Scripted([Decision(1.0, reason="go")]), maker_offset_bps=50)
        eng.process(candle(1, 100, 100, 100, 100))
        eng.process(candle(2, 100, 101, 99, 100))
        eng.close_position(100.0, 3 * MINUTE, "risk halt")
        self.assertTrue(pf.is_flat)
        self.assertEqual(pf.fills[-1].liquidity, Liquidity.TAKER)


class TestOffByDefault(unittest.TestCase):
    def test_the_default_is_market_orders(self):
        self.assertEqual(ExecutionSettings().maker_offset_bps, 0.0)

    def test_with_maker_off_nothing_ever_rests(self):
        eng, pf = engine(Scripted([Decision(1.0, reason="go")]))
        eng.process(candle(1, 100, 100, 100, 100))
        eng.process(candle(2, 100, 101, 99, 100))
        self.assertIsNone(eng.resting)
        self.assertEqual(eng.maker_fills, 0)
        self.assertEqual(pf.fills[-1].liquidity, Liquidity.TAKER)


if __name__ == "__main__":
    unittest.main()


class TestFeesAreBilledToTheRightSide(unittest.TestCase):
    """A fill must be charged for what it actually did to the book.

    Billing every fill at the maker rate halves the cost of the entire package at a
    stroke, and it flatters every backtest in it. This escaped a mutation sweep, which
    is exactly the kind of error that ships: it makes the numbers better, so nothing
    complains.
    """

    def rates(self):
        return CostModel(taker_fee_bps=40, maker_fee_bps=10, half_spread_bps=0, slippage_bps=0)

    def test_a_crossing_order_pays_the_taker_rate(self):
        eng, pf = engine(Scripted([Decision(1.0, reason="go")]), costs=self.rates())
        eng.process(candle(1, 100, 100, 100, 100))
        eng.process(candle(2, 100, 101, 99, 100))
        fill = pf.fills[-1]
        self.assertEqual(fill.liquidity, Liquidity.TAKER)
        self.assertAlmostEqual(fill.fee, fill.qty * fill.price * 40e-4, places=6)

    def test_a_resting_order_pays_the_maker_rate(self):
        eng, pf = engine(Scripted([Decision(1.0, reason="go")]),
                         costs=self.rates(), maker_offset_bps=50)
        eng.process(candle(1, 100, 100, 100, 100))
        eng.process(candle(2, 100, 101, 99, 100))
        fill = pf.fills[-1]
        self.assertEqual(fill.liquidity, Liquidity.MAKER)
        self.assertAlmostEqual(fill.fee, fill.qty * fill.price * 10e-4, places=6)

    def test_the_two_rates_are_actually_different(self):
        """Guards the guard: if both rates billed the same, both tests above would pass."""
        taker_eng, taker_pf = engine(Scripted([Decision(1.0, reason="go")]), costs=self.rates())
        taker_eng.process(candle(1, 100, 100, 100, 100))
        taker_eng.process(candle(2, 100, 101, 99, 100))

        maker_eng, maker_pf = engine(Scripted([Decision(1.0, reason="go")]),
                                     costs=self.rates(), maker_offset_bps=50)
        maker_eng.process(candle(1, 100, 100, 100, 100))
        maker_eng.process(candle(2, 100, 101, 99, 100))

        self.assertGreater(taker_pf.fees_paid, maker_pf.fees_paid * 2,
                           "crossing the spread should cost far more than resting")

    def test_a_missed_limit_that_crosses_late_pays_taker(self):
        eng, pf = engine(Scripted([Decision(1.0, reason="go")]),
                         costs=self.rates(), maker_offset_bps=50, maker_then_take=True)
        eng.process(candle(1, 100, 100, 100, 100))
        eng.process(candle(2, 100, 108, 100, 107))
        fill = pf.fills[-1]
        self.assertEqual(fill.liquidity, Liquidity.TAKER)
        self.assertAlmostEqual(fill.fee, fill.qty * fill.price * 40e-4, places=6)


class TestSellsRestToo(unittest.TestCase):
    """Everything above tested buys. A sell rests on the other side and can rot there.

    A resting sell sits *above* the market and fills only if price rises to it. So an
    exit that is routed passively does not happen while price is falling - which is
    the moment you most wanted it. Getting in passively is a saving; getting out
    passively is a hazard, and it is why bracket exits never rest.
    """

    def held(self, **ex):
        """An engine already holding a full position, ready to be reduced."""
        eng, pf = engine(Scripted([Decision(1.0, reason="in"), Decision(0.5, reason="trim")]), **ex)
        eng.process(candle(1, 100, 100, 100, 100))
        eng.process(candle(2, 100, 101, 99, 100))
        return eng, pf

    def test_a_sell_rests_above_the_market(self):
        eng, pf = self.held(maker_offset_bps=50, maker_then_take=False)
        held = pf.qty
        eng.process(candle(3, 100, 100.2, 99, 100))   # never rose to 100.50
        self.assertAlmostEqual(pf.qty, held, msg="a sell filled without price reaching it")
        self.assertEqual(eng.maker_misses, 1)

    def test_a_sell_fills_when_price_rises_to_it(self):
        eng, pf = self.held(maker_offset_bps=50)
        held = pf.qty
        eng.process(candle(3, 100, 102, 99, 100))
        self.assertLess(pf.qty, held)
        self.assertEqual(pf.fills[-1].liquidity, Liquidity.MAKER)

    def test_the_queue_applies_to_sells_as_well_as_buys(self):
        """Otherwise half the orders are modelled optimistically and half are not."""
        eng, pf = self.held(maker_offset_bps=50, maker_queue_bps=40, maker_then_take=False)
        held = pf.qty
        eng.process(candle(3, 100, 100.5, 99, 100))   # exactly touches, does not push through
        self.assertAlmostEqual(pf.qty, held,
                               msg="a sell filled from the back of the queue on a single touch")

    def test_a_sell_trading_well_through_still_fills_with_a_queue(self):
        eng, pf = self.held(maker_offset_bps=50, maker_queue_bps=40)
        held = pf.qty
        eng.process(candle(3, 100, 103, 99, 100))
        self.assertLess(pf.qty, held)
