"""Trend following: hold while a fast average leads a slow one.

Trend following is the opposite of scalping in the one way that matters. It trades
rarely and holds for a long time, so the round-trip cost is amortised over a move
that may be worth several percent. That does not make it profitable - most of the
time it is not, and it loses money in choppy, directionless markets by design - but
at least costs are not guaranteed to eat the edge before it exists.

The ATR-based stop is what keeps a single bad trend from turning into a large loss.
"""

from __future__ import annotations

from ..indicators import ATR, EMA
from ..types import Candle, Decision
from .base import Context, Strategy, register


@register
class EmaCross(Strategy):
    """Long while the fast EMA is above the slow EMA, flat otherwise, ATR stop."""

    name = "ema_cross"

    def __init__(
        self,
        fast: int = 20,
        slow: int = 50,
        atr_period: int = 14,
        stop_atr_multiple: float = 2.5,
        size: float = 1.0,
        min_separation_pct: float = 0.001,
    ) -> None:
        if fast >= slow:
            raise ValueError("fast period must be shorter than slow period")
        self.fast = EMA(fast)
        self.slow = EMA(slow)
        self.atr = ATR(atr_period)
        self.stop_atr_multiple = stop_atr_multiple
        self.size = size
        self.min_separation_pct = min_separation_pct
        self._slow_period = slow
        self._trail: float | None = None

    @property
    def warmup(self) -> int:
        return self._slow_period

    def on_candle(self, candle: Candle, ctx: Context) -> Decision:
        fast = self.fast.update(candle.close)
        slow = self.slow.update(candle.close)
        atr = self.atr.update(candle)

        if fast is None or slow is None:
            return Decision(0.0, reason="warming up")

        separation = (fast - slow) / slow

        # The stop ratchets: it follows price up and never moves back down. Recomputing
        # it from the latest close each bar - the obvious implementation - lets the stop
        # fall with the price, so it retreats exactly when it is supposed to be catching
        # you, and a position can bleed indefinitely without ever triggering it.
        if ctx.is_flat:
            self._trail = None
        if atr is not None:
            candidate = candle.close - self.stop_atr_multiple * atr
            self._trail = candidate if self._trail is None else max(self._trail, candidate)
        stop = self._trail

        # A band around the crossover stops the strategy flip-flopping every bar when
        # the two averages sit on top of each other - each of those flips would cost a
        # full round trip for no directional view at all.
        if separation > self.min_separation_pct:
            return Decision(
                target_weight=self.size,
                stop_loss=stop,
                reason=f"uptrend, fast {separation:+.2%} above slow",
            )
        if separation < -self.min_separation_pct:
            return Decision(0.0, reason=f"downtrend, fast {separation:+.2%} below slow")

        return Decision(
            target_weight=None,
            stop_loss=stop if ctx.exposure > 0 else None,
            reason="averages too close to call, holding current position",
        )
