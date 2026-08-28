"""Hold while price is above a long moving average, sit in cash when it is below.

Every active strategy in this package loses to buy-and-hold, and the measured reason
is cost drag: they trade hundreds or thousands of times and pay 20-150% of capital a
year for the privilege. This one exists to test the obvious follow-up question - can
you keep a trend filter's drawdown protection while trading rarely enough that the
costs round to nothing?

It is the oldest idea in tactical allocation: stay invested while the market is above
its long-run average, step aside when it drops below. On daily bars with a 200-day
window it trades a handful of times a decade, so its cost drag is a rounding error
next to a 15-minute strategy's.

What it cannot do is predict anything. It reacts, always late, and in a choppy market
it will sell the bottom and buy back higher - repeatedly. The honest case for it is
not higher returns, it is a shallower worst case, and whether it delivers even that
is what the study is for rather than something to take on faith.
"""

from __future__ import annotations

from ..indicators import EMA
from ..types import Candle, Decision
from .base import Context, Strategy, register


@register
class SlowTrend(Strategy):
    """Holds above a long moving average, cash below it. Trades rarely, so costs stay small."""

    name = "slow_trend"

    def __init__(
        self,
        period: int = 200,
        band_pct: float = 0.02,
        size: float = 1.0,
    ) -> None:
        """
        ``band_pct`` is a buffer around the average. Without it, price hovering on the
        line produces a burst of in-out trades that pay full costs for no view at all -
        the exact failure this strategy is meant to avoid.
        """
        if period < 2:
            raise ValueError("period must be at least 2")
        if band_pct < 0:
            raise ValueError("band_pct must not be negative")
        self.average = EMA(period)
        self.band_pct = band_pct
        self.size = size
        self._period = period

    @property
    def warmup(self) -> int:
        return self._period

    def on_candle(self, candle: Candle, ctx: Context) -> Decision:
        average = self.average.update(candle.close)
        if average is None:
            return Decision(0.0, reason="warming up")

        above = candle.close > average * (1.0 + self.band_pct)
        below = candle.close < average * (1.0 - self.band_pct)
        distance = (candle.close / average - 1.0) * 100.0

        if above:
            return Decision(self.size, reason=f"above trend ({distance:+.1f}%)")
        if below:
            return Decision(0.0, reason=f"below trend ({distance:+.1f}%)")
        return Decision(None, reason=f"inside the band ({distance:+.1f}%), holding")
