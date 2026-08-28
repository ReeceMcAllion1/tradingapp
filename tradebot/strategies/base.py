"""Strategy interface.

A strategy sees one bar at a time and returns the position it wants to hold. It
does not place orders, size positions against the account, or know anything about
fees - the engine and the risk manager own those. Keeping strategies this small
means a strategy can be unit tested with a handful of made-up candles.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..costs import CostModel
from ..types import Candle, Decision


@dataclass(frozen=True)
class Context:
    """What the strategy is allowed to know about the account."""

    exposure: float
    equity: float
    costs: CostModel
    avg_price: float = 0.0

    @property
    def is_flat(self) -> bool:
        return abs(self.exposure) < 1e-9

    @property
    def hold_weight(self) -> float:
        """Current exposure, clamped to a weight a ``Decision`` will accept.

        Use this, not ``exposure``, when a strategy wants to keep the position it
        already has. Measured exposure sits a fraction above 1.0 while fully invested,
        because entry fees leave cash slightly negative.
        """
        return max(-1.0, min(1.0, self.exposure))

    @property
    def breakeven_move_pct(self) -> float:
        """Percentage move needed to cover a round trip. Ignore this at your peril."""
        return self.costs.breakeven_move_pct()


class Strategy(ABC):
    """Base class for all strategies."""

    name: str = "unnamed"

    @property
    def warmup(self) -> int:
        """Bars of history needed before the strategy's output is meaningful."""
        return 0

    @abstractmethod
    def on_candle(self, candle: Candle, ctx: Context) -> Decision:
        """Return the position wanted after this bar closes."""

    def cost_warnings(self, costs: CostModel) -> list[str]:
        """Problems with this strategy's parameters given the venue's costs.

        Checked before every run and printed prominently. A strategy that knows its
        own profit target can say here whether that target is even reachable - which
        is far more use to you before the run than after it.
        """
        return []

    def describe(self) -> str:
        return self.__doc__.strip().splitlines()[0] if self.__doc__ else self.name


_REGISTRY: dict[str, type[Strategy]] = {}


def register(cls: type[Strategy]) -> type[Strategy]:
    _REGISTRY[cls.name] = cls
    return cls


def available() -> dict[str, type[Strategy]]:
    return dict(_REGISTRY)


def build(name: str, **params) -> Strategy:
    if name not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY)) or "none registered"
        raise KeyError(f"unknown strategy {name!r}; available: {known}")
    return _REGISTRY[name](**params)
