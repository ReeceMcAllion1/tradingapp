"""Buy and hold: the benchmark every other strategy has to beat.

This is the most important strategy in the package, and it is four lines long.

Any active strategy has to be measured against simply buying the thing and doing
nothing, because that is the alternative genuinely available to you. A strategy that
returns 60% over ten years has not made you money if buying and holding returned
200% over the same period - it has cost you 140%, plus the years you spent watching
it.

Buy and hold is also very hard to beat, for reasons that have nothing to do with
cleverness: it pays two lots of costs in a decade instead of two per week, it never
sits in cash while the market rises, and it cannot be stopped out at the bottom. Most
professional fund managers underperform it over ten years. There is no reason to
assume a script will do better.
"""

from __future__ import annotations

from ..types import Candle, Decision
from .base import Context, Strategy, register


@register
class BuyAndHold(Strategy):
    """Buys once and never sells. The benchmark every strategy must beat."""

    name = "buy_and_hold"

    def __init__(self, size: float = 1.0) -> None:
        self.size = size

    def on_candle(self, candle: Candle, ctx: Context) -> Decision:
        return Decision(target_weight=self.size, reason="hold")
