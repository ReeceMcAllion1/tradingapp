"""Never sell at a loss. Hold until the trade is in profit, however long that takes.

This is the "only cash out if it makes the slightest amount of money" rule, built
exactly as asked: buy, then hold - through any drawdown, for any length of time -
and sell the moment the position is worth more than it cost. There is no stop loss.
Selling at a loss is the one thing this strategy will never do.

It is worth being clear about what that buys you and what it costs you, because the
appeal is real and so is the trap.

**Why it looks brilliant.** Almost every trade is a winner. The win rate runs close
to 100%, because a losing trade is never closed and so never appears in the results.
The trade log is a wall of green. In a rising market it also genuinely makes money.

**Why the win rate is meaningless.** It is an artefact of the rule, not evidence
about the market. You can guarantee a 100% win rate on any asset by refusing to
realise losses; the losses do not disappear, they just stop being counted. Money lost
on paper is money lost. "I haven't sold, so I haven't lost" is an accounting story,
not a fact about your net worth - and it is the single most documented mistake in
retail trading, the disposition effect: investors sell winners too early and hold
losers too long, and it costs them measurably.

**What it actually does to your risk.** A stop loss caps the downside and lets the
upside run. This rule does precisely the opposite. The upside is capped, at the tiny
profit target you set, while the downside is unlimited until the position recovers -
or does not. You have inverted the asymmetry that makes trading survivable, and you
have done it deliberately.

**The failure mode that ends it.** The rule assumes recovery. Every position comes
back eventually, so you always get your small win. That assumption holds for a broad
index over a long horizon and it does not hold for individual companies. First
Republic Bank went from $220 to zero in 2023. Peloton fell 97% and has not returned.
A "never sell at a loss" position in either is not a trade waiting to close - it is a
permanent loss of capital, held to the end by a rule that would not let go.

Meanwhile the capital is locked. While you wait years for one position to crawl back
to break-even, that money is doing nothing else.

Run ``python -m tradebot study --strategies never_lose`` to see all of this on ten
years of real prices, including the stocks that never came back.
"""

from __future__ import annotations

from ..costs import CostModel
from ..types import Candle, Decision
from .base import Context, Strategy, register


@register
class NeverLose(Strategy):
    """Never sells at a loss - holds until profitable. High win rate, unlimited downside."""

    name = "never_lose"

    def __init__(
        self,
        min_profit_pct: float = 0.0,
        size: float = 1.0,
        gross: bool = False,
    ) -> None:
        """
        ``min_profit_pct`` is profit demanded *on top of* covering all costs, so the
        default of 0.0 means "sell the instant the trade nets anything at all".

        ``gross`` switches the target to the naive reading - sell as soon as the price
        is above what you paid, ignoring costs. That version books a "win" that is
        really a small net loss, so it is off by default; turn it on to see the
        difference the distinction makes.
        """
        if min_profit_pct < 0:
            raise ValueError("min_profit_pct must not be negative - this strategy never takes a loss")
        self.min_profit_pct = min_profit_pct
        self.size = size
        self.gross = gross

    def cost_warnings(self, costs: CostModel) -> list[str]:
        return [
            "never_lose has no stop loss by design: a position that keeps falling is "
            "held indefinitely, and its win rate will look near-perfect because losing "
            "trades are never closed and so never counted."
        ]

    def on_candle(self, candle: Candle, ctx: Context) -> Decision:
        if ctx.is_flat:
            # No stop_loss, ever. That is the whole strategy.
            return Decision(self.size, reason="buy - will not sell at a loss")

        if self.gross:
            target = ctx.avg_price
        else:
            qty = abs(ctx.exposure) * ctx.equity / candle.close if candle.close else None
            target = ctx.costs.net_breakeven_exit(ctx.avg_price, qty)
        target *= 1.0 + self.min_profit_pct

        underwater = (candle.close / ctx.avg_price - 1.0) * 100.0 if ctx.avg_price else 0.0
        reason = (
            f"holding for break-even ({underwater:+.1f}% vs entry)"
            if underwater < 0
            else f"waiting to clear costs ({underwater:+.1f}% vs entry)"
        )
        return Decision(None, take_profit=target, reason=reason)
