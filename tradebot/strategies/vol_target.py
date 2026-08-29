"""Hold the market, but size the position by how violent it currently is.

Everything else in this package tries to time direction, and walk-forward says none of
it transfers: across eight markets and forty-eight folds, ten folds beat holding and
every single market lost to it on median. The one effect that did survive was smaller
drawdowns, in every market, without exception.

So this strategy stops guessing direction and leans on the thing that measured real.
It forecasts nothing. It holds the market all the time and only decides *how much*,
from realised volatility: when the market is calmer than the risk target, hold a full
position; when it is wilder, hold less. That is the whole idea.

Why it might work where trend-timing did not:

* It does not need to be right about the future. Volatility is the one property of
  markets that genuinely persists - a turbulent week is followed by a turbulent week
  far more often than chance. Direction is not remotely so obliging.
* It stays invested. A trend filter's worst habit is sitting in cash through a rally
  it sold the bottom of; this never leaves, it only shrinks.
* Crashes are volatile. Cutting exposure when volatility spikes is what produced the
  drawdown reduction in the first place - this targets it directly rather than as a
  side effect of a direction call.

Why it still might not:

* Volatility spikes on the way *up* too, and this will shrink into good days.
* An average position below 100% must, in a rising market, return less than holding
  100%. Judge it on return per unit of risk, not on return alone - and the comparison
  the study prints against buy-and-hold is deliberately unkind to it on that count.
* Resizing costs money. The whole reason every other strategy here fails is cost
  drag, so the position is quantised into coarse steps and only moved when the change
  is worth paying for. Without that this becomes another fee pump.

None of the above is a reason to believe it. The walk-forward number is.
"""

from __future__ import annotations

import math

from ..indicators import EMA, RollingStats
from ..types import Candle, Decision
from .base import Context, Strategy, register

#: Bars per year, used to annualise realised volatility. Inferred from bar spacing.
MS_PER_YEAR = 365.25 * 24 * 60 * 60 * 1000


@register
class VolTarget(Strategy):
    """Always invested, sized by realised volatility. Forecasts nothing; only shrinks when wild."""

    name = "vol_target"

    def __init__(
        self,
        lookback: int = 30,
        target_vol: float = 0.15,
        max_weight: float = 1.0,
        step: float = 0.2,
        trend_period: int = 0,
    ) -> None:
        """
        ``lookback`` is how many bars of returns the volatility estimate uses. Short
        enough to react to a regime change, long enough not to be noise itself.

        ``target_vol`` is the annualised volatility to aim at, as a fraction. 0.15 is
        roughly a broad equity index's long-run figure, so on a typical market this
        holds close to a full position and only shrinks when things get worse than
        normal.

        ``step`` quantises the position into coarse rungs - at 0.2 the weight can only
        be 0, 0.2, 0.4 ... so ordinary wobble in the volatility estimate does not
        produce a trade. This is the cost control, and it is load-bearing: without it
        the weight changes every bar and the fees eat the strategy alive, which is the
        documented cause of death for everything else in this package.

        ``trend_period`` optionally adds a long moving-average filter on top, going to
        cash below it. Zero - the default - disables it, because the point of this
        strategy is to test volatility sizing *on its own*, without a direction call
        smuggled in to take the credit.
        """
        if lookback < 2:
            raise ValueError("lookback must be at least 2")
        if target_vol <= 0:
            raise ValueError("target_vol must be positive")
        if not 0 < max_weight <= 1.0:
            raise ValueError("max_weight must be in (0, 1]")
        if not 0 < step <= 1.0:
            raise ValueError("step must be in (0, 1]")
        if trend_period and trend_period < 2:
            raise ValueError("trend_period must be at least 2, or 0 to disable")

        self.returns = RollingStats(lookback)
        self.target_vol = target_vol
        self.max_weight = max_weight
        self.step = step
        self.trend = EMA(trend_period) if trend_period else None

        self._lookback = lookback
        self._trend_period = trend_period
        self._prev_close: float | None = None
        self._prev_ts: int | None = None
        self._bars_per_year: float | None = None

    @property
    def warmup(self) -> int:
        return max(self._lookback, self._trend_period) + 1

    def _observe_spacing(self, ts: int) -> None:
        """Learn the bar size from the data, so this works on any timeframe untold."""
        if self._prev_ts is not None and self._bars_per_year is None:
            gap = ts - self._prev_ts
            if gap > 0:
                self._bars_per_year = MS_PER_YEAR / gap
        self._prev_ts = ts

    def on_candle(self, candle: Candle, ctx: Context) -> Decision:
        self._observe_spacing(candle.ts)

        average = self.trend.update(candle.close) if self.trend else None

        previous, self._prev_close = self._prev_close, candle.close
        if previous is None or previous <= 0:
            return Decision(0.0, reason="warming up")

        stats = self.returns.update(candle.close / previous - 1.0)
        if stats is None or self._bars_per_year is None:
            return Decision(0.0, reason="warming up")

        _, stdev = stats
        annualised = stdev * math.sqrt(self._bars_per_year)

        if self.trend is not None:
            if average is None:
                return Decision(0.0, reason="warming up")
            if candle.close < average:
                return Decision(0.0, reason=f"below trend, vol {annualised:.0%}")

        if annualised <= 0:
            weight = self.max_weight
        else:
            weight = min(self.max_weight, self.target_vol / annualised)

        # Coarse rungs, rounded down: never hold more risk than the target allows.
        rungs = math.floor(weight / self.step)
        quantised = min(self.max_weight, rungs * self.step)

        if quantised <= 0:
            return Decision(0.0, reason=f"vol {annualised:.0%} too high to hold")
        return Decision(quantised, reason=f"vol {annualised:.0%}, hold {quantised:.0%}")

    def cost_warnings(self, costs) -> list[str]:
        notes = []
        if self.step < 0.1:
            notes.append(
                f"step is {self.step:.2f}: the position will be adjusted on small changes "
                f"in volatility, and each adjustment pays {costs.round_trip_bps:.0f} bps. "
                "Cost drag is what kills every other strategy here - use a coarser step."
            )
        return notes
