"""The "take any profit, even a few pence" strategy - included so you can see it fail.

This is the strategy people ask for first: buy, then sell the moment the position is
up by a tiny fixed amount. It is implemented faithfully and honestly here, and it
loses money on essentially any real price series, for a reason that has nothing to
do with how good the entry signal is.

A round trip costs you the spread twice, slippage twice, and commission twice. On
the conservative defaults in ``CostModel`` that is 28 basis points - 0.28% of the
position:

    £100 position   -> 5p target,   28p of costs -> lose 23p per trade
    £1,000 position -> 5p target, £2.80 of costs -> lose £2.75 per trade

Being precise about this, because the honest version is more useful than the scary
one: a *fixed cash* target is not hopeless at every size. Cost scales with position
while the target does not, so 5p does clear 0.28% on any position under about £18.
That is the entire loophole, and it is not much of one - you would be tying up £18 to
earn 5p, needing a 0.28% move to do it, and most venues will not accept an order that
small anyway. Twenty such wins a day, with no losers at all, is £1.

What has no loophole is a target expressed as a *percentage* below the round-trip
cost, which is what this strategy uses and what people actually mean by "take any
profit". Both sides then scale together and position size cancels out, so it loses at
every size, forever. The fix is not a smaller position - it is a bigger target.

Run ``python -m tradebot demo`` to see this on real data.
"""

from __future__ import annotations

from ..costs import CostModel
from ..types import Candle, Decision
from .base import Context, Strategy, register


@register
class MicroScalp(Strategy):
    """Buys on any dip and sells for a tiny fixed profit. Demonstrates cost drag."""

    name = "micro_scalp"

    def __init__(
        self,
        profit_target_pct: float = 0.0005,
        stop_pct: float = 0.02,
        dip_pct: float = 0.0,
        size: float = 1.0,
    ) -> None:
        self.profit_target_pct = profit_target_pct
        self.stop_pct = stop_pct
        self.dip_pct = dip_pct
        self.size = size
        self._prev_close: float | None = None

    def cost_warnings(self, costs: CostModel) -> list[str]:
        target_pct = self.profit_target_pct * 100.0
        breakeven_pct = costs.breakeven_move_pct()
        if target_pct >= breakeven_pct:
            return []
        shortfall = breakeven_pct - target_pct
        return [
            f"profit target is {target_pct:.3f}% but a round trip costs {breakeven_pct:.3f}% - "
            f"every winning trade will still lose {shortfall:.3f}% of the position. "
            f"The target needs to be at least {breakeven_pct:.3f}% to break even."
        ]

    def on_candle(self, candle: Candle, ctx: Context) -> Decision:
        prev = self._prev_close
        self._prev_close = candle.close

        if ctx.is_flat:
            if prev is None:
                return Decision(0.0, reason="warming up")
            dipped = candle.close <= prev * (1.0 - self.dip_pct)
            if not dipped:
                return Decision(0.0, reason="waiting for a dip")
            return Decision(
                target_weight=self.size,
                stop_loss=candle.close * (1.0 - self.stop_pct),
                take_profit=candle.close * (1.0 + self.profit_target_pct),
                reason=f"scalp entry, target +{self.profit_target_pct:.4%}",
            )

        # Already long: hold and let the engine's bracket exits do the work. The
        # take-profit is what makes this a scalper, and what makes it lose.
        return Decision(
            target_weight=self.size,
            stop_loss=ctx.avg_price * (1.0 - self.stop_pct),
            take_profit=ctx.avg_price * (1.0 + self.profit_target_pct),
            reason="holding for scalp target",
        )
