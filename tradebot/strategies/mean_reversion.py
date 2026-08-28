"""Mean reversion with an explicit cost filter.

Buys when price is unusually far below its recent average and the expected snap-back
is several times bigger than the round-trip cost. That last clause is the entire
point: the strategy asks "is this move worth paying the spread for?" before every
entry, and refuses most of them.

This is what a scalper should look like once it has been made to respect costs. It
trades far less often than ``micro_scalp`` and targets moves that are large enough
to survive them.
"""

from __future__ import annotations

from ..indicators import ATR, RollingStats
from ..types import Candle, Decision
from .base import Context, Strategy, register


@register
class MeanReversion(Strategy):
    """Buys statistically cheap dips, but only when the target move clears costs."""

    name = "mean_reversion"

    def __init__(
        self,
        lookback: int = 48,
        entry_z: float = -2.0,
        exit_z: float = -0.25,
        atr_period: int = 14,
        stop_atr_multiple: float = 2.0,
        size: float = 1.0,
        min_edge_multiple: float = 3.0,
    ) -> None:
        if entry_z >= 0:
            raise ValueError("entry_z should be negative - it marks an unusually low price")
        self.stats = RollingStats(lookback)
        self.atr = ATR(atr_period)
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.stop_atr_multiple = stop_atr_multiple
        self.size = size
        self.min_edge_multiple = min_edge_multiple
        self._lookback = lookback

    @property
    def warmup(self) -> int:
        return self._lookback

    def on_candle(self, candle: Candle, ctx: Context) -> Decision:
        atr = self.atr.update(candle)
        # Measure this bar against the window of bars *before* it, then fold it in.
        # Doing it the other way round would let the strategy see its own close in
        # the average it is being compared to.
        zscore = self.stats.zscore(candle.close)
        window_mean = self.stats.mean
        self.stats.update(candle.close)

        if zscore is None or window_mean is None:
            return Decision(0.0, reason="warming up")

        if not ctx.is_flat:
            if zscore >= self.exit_z:
                return Decision(0.0, reason=f"reverted to mean (z={zscore:+.2f})")
            stop = None
            if atr is not None:
                stop = ctx.avg_price - self.stop_atr_multiple * atr
            return Decision(self.size, stop_loss=stop, reason=f"waiting for reversion (z={zscore:+.2f})")

        if zscore > self.entry_z:
            return Decision(0.0, reason=f"not stretched enough (z={zscore:+.2f})")

        # The trade is only worth taking if the move back to the mean is several times
        # the cost of getting in and out. Without this check the strategy would take
        # dozens of technically-correct signals that all net out negative.
        expected_move_pct = abs(window_mean - candle.close) / candle.close * 100.0
        required = self.min_edge_multiple * ctx.breakeven_move_pct
        if expected_move_pct < required:
            return Decision(
                0.0,
                reason=(
                    f"edge too thin: {expected_move_pct:.3f}% expected vs "
                    f"{required:.3f}% needed to beat costs"
                ),
            )

        stop = None
        if atr is not None:
            stop = candle.close - self.stop_atr_multiple * atr
        return Decision(
            target_weight=self.size,
            stop_loss=stop,
            reason=f"stretched low (z={zscore:+.2f}), {expected_move_pct:.3f}% to mean",
        )
