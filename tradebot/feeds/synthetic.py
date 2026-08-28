"""A synthetic price series, for tests and for the offline demo.

This generates a random walk with realistic intrabar ranges and occasional volatility
clustering. It is not a market and nothing learned from it transfers to one - its
only jobs are to make the test suite deterministic and to let ``tradebot demo`` run
without a network connection.

The cost arithmetic the demo illustrates does not depend on the price path at all,
which is exactly why a random series is enough to make the point.
"""

from __future__ import annotations

import random

from ..types import Candle
from .base import Feed, validate_series

FIVE_MINUTES_MS = 5 * 60 * 1000


class SyntheticFeed(Feed):
    def __init__(
        self,
        bars: int = 5000,
        start_price: float = 30_000.0,
        drift_per_bar: float = 0.0,
        volatility_per_bar: float = 0.0025,
        interval_ms: int = FIVE_MINUTES_MS,
        seed: int = 7,
        start_ts: int = 1_700_000_000_000,
    ) -> None:
        self.bars = bars
        self.start_price = start_price
        self.drift = drift_per_bar
        self.volatility = volatility_per_bar
        self.interval_ms = interval_ms
        self.seed = seed
        self.start_ts = start_ts

    def history(self, limit: int) -> list[Candle]:
        return self.generate()[-limit:]

    def generate(self) -> list[Candle]:
        rng = random.Random(self.seed)
        candles: list[Candle] = []
        price = self.start_price
        vol = self.volatility

        for index in range(self.bars):
            # Volatility clusters: calm stretches and violent ones, like the real thing.
            vol = max(0.0004, vol * (0.97 + 0.06 * rng.random()))
            open_ = price
            close = max(0.01, open_ * (1.0 + self.drift + rng.gauss(0.0, vol)))
            wick = abs(rng.gauss(0.0, vol)) * open_
            high = max(open_, close) + wick * rng.random()
            low = max(0.01, min(open_, close) - wick * rng.random())
            candles.append(
                Candle(
                    ts=self.start_ts + index * self.interval_ms,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=abs(rng.gauss(50.0, 15.0)) + 1.0,
                )
            )
            price = close

        return validate_series(candles)
