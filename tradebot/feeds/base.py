"""Market data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from ..types import Candle


class Feed(ABC):
    """A source of OHLCV bars."""

    @abstractmethod
    def history(self, limit: int) -> list[Candle]:
        """Return up to ``limit`` recent closed bars, oldest first."""

    def stream(self) -> Iterator[Candle]:
        """Yield new bars as they close. Only live feeds implement this."""
        raise NotImplementedError(f"{type(self).__name__} does not support streaming")


def validate_series(candles: list[Candle]) -> list[Candle]:
    """Sort by time, drop duplicate timestamps, and reject an empty series.

    Exchange APIs sometimes return bars out of order or repeat the most recent one
    across pages. Feeding those to a strategy produces silent nonsense, so every feed
    passes its output through here.
    """
    if not candles:
        raise ValueError("feed returned no candles")
    ordered = sorted(candles, key=lambda c: c.ts)
    deduped: list[Candle] = []
    for candle in ordered:
        if deduped and candle.ts == deduped[-1].ts:
            deduped[-1] = candle
            continue
        deduped.append(candle)
    return deduped
