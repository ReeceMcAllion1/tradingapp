"""Streaming indicators.

Each indicator is fed one bar at a time and holds its own state, so a live run and
a backtest produce identical values from identical data. That matters more than it
sounds: indicators written as "compute over the whole array" are the usual source of
look-ahead bias, where a backtest quietly peeks at prices that had not happened yet.

Every indicator returns ``None`` until it has enough history to be meaningful.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .types import Candle


@dataclass
class EMA:
    """Exponential moving average, seeded with a simple average of the first period."""

    period: int
    value: float | None = field(default=None, init=False)
    _seed: list[float] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        if self.period < 1:
            raise ValueError("period must be at least 1")
        self._alpha = 2.0 / (self.period + 1.0)

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, price: float) -> float | None:
        if self.value is None:
            self._seed.append(price)
            if len(self._seed) < self.period:
                return None
            self.value = sum(self._seed) / len(self._seed)
            self._seed.clear()
            return self.value
        self.value += self._alpha * (price - self.value)
        return self.value


@dataclass
class ATR:
    """Average true range (Wilder), a volatility measure in price units."""

    period: int = 14
    value: float | None = field(default=None, init=False)
    _prev_close: float | None = field(default=None, init=False)
    _seed: list[float] = field(default_factory=list, init=False)

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, candle: Candle) -> float | None:
        if self._prev_close is None:
            true_range = candle.high - candle.low
        else:
            true_range = max(
                candle.high - candle.low,
                abs(candle.high - self._prev_close),
                abs(candle.low - self._prev_close),
            )
        self._prev_close = candle.close

        if self.value is None:
            self._seed.append(true_range)
            if len(self._seed) < self.period:
                return None
            self.value = sum(self._seed) / len(self._seed)
            self._seed.clear()
            return self.value

        self.value = (self.value * (self.period - 1) + true_range) / self.period
        return self.value


@dataclass
class RollingStats:
    """Mean and population standard deviation over a fixed window."""

    period: int
    _window: deque[float] = field(default_factory=deque, init=False)

    def __post_init__(self) -> None:
        if self.period < 2:
            raise ValueError("period must be at least 2")
        self._window = deque(maxlen=self.period)

    @property
    def ready(self) -> bool:
        return len(self._window) == self.period

    def update(self, value: float) -> tuple[float, float] | None:
        self._window.append(value)
        if not self.ready:
            return None
        mean = sum(self._window) / len(self._window)
        variance = sum((x - mean) ** 2 for x in self._window) / len(self._window)
        return mean, variance**0.5

    @property
    def mean(self) -> float | None:
        if not self.ready:
            return None
        return sum(self._window) / len(self._window)

    def zscore(self, value: float) -> float | None:
        """How many standard deviations ``value`` sits from the window mean."""
        if not self.ready:
            return None
        mean = sum(self._window) / len(self._window)
        variance = sum((x - mean) ** 2 for x in self._window) / len(self._window)
        stdev = variance**0.5
        if stdev < 1e-12:
            return 0.0
        return (value - mean) / stdev


@dataclass
class RSI:
    """Relative strength index (Wilder), 0-100."""

    period: int = 14
    value: float | None = field(default=None, init=False)
    _prev: float | None = field(default=None, init=False)
    _avg_gain: float = field(default=0.0, init=False)
    _avg_loss: float = field(default=0.0, init=False)
    _count: int = field(default=0, init=False)

    @property
    def ready(self) -> bool:
        return self.value is not None

    def update(self, price: float) -> float | None:
        if self._prev is None:
            self._prev = price
            return None
        change = price - self._prev
        self._prev = price
        gain = max(change, 0.0)
        loss = max(-change, 0.0)

        self._count += 1
        if self._count <= self.period:
            self._avg_gain += gain / self.period
            self._avg_loss += loss / self.period
            if self._count < self.period:
                return None
        else:
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period

        if self._avg_loss < 1e-12:
            self.value = 100.0
        else:
            rs = self._avg_gain / self._avg_loss
            self.value = 100.0 - (100.0 / (1.0 + rs))
        return self.value
