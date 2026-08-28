"""Core value objects shared by the feeds, strategies, brokers and engines."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Liquidity(str, Enum):
    """Whether an order crossed the spread (taker) or rested on the book (maker)."""

    TAKER = "taker"
    MAKER = "maker"


@dataclass(frozen=True)
class Candle:
    """One OHLCV bar. ``ts`` is the bar's *open* time in epoch milliseconds."""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise ValueError(f"candle at {self.ts}: high {self.high} < low {self.low}")
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"candle at {self.ts}: {name} must be a positive number, got {value}")

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3.0


@dataclass(frozen=True)
class Decision:
    """What a strategy wants the portfolio to look like after this bar.

    ``target_weight`` is the fraction of account equity to hold in the instrument:
    ``0.0`` is flat, ``1.0`` is fully long, ``-0.5`` is half-size short. Expressing
    intent as a target rather than as buy/sell orders means a strategy cannot
    accidentally double up or leak a position when a bar is missed or replayed.
    """

    target_weight: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not math.isfinite(self.target_weight):
            raise ValueError("target_weight must be finite")
        if abs(self.target_weight) > 1.0:
            raise ValueError(f"target_weight {self.target_weight} outside [-1, 1]")


@dataclass(frozen=True)
class Fill:
    """An executed order.

    ``price`` is what you actually paid or received. ``reference_price`` is the mid
    price the order was priced from, before spread and slippage were applied against
    you. Keeping both is what lets the reports separate "the market moved" from "the
    venue took a cut" - without the reference price, spread cost hides inside the
    profit figure and a losing strategy can look like a winning one.
    """

    ts: int
    side: Side
    qty: float
    price: float
    fee: float
    reference_price: float = 0.0
    liquidity: Liquidity = Liquidity.TAKER
    reason: str = ""

    @property
    def notional(self) -> float:
        return self.qty * self.price

    @property
    def signed_qty(self) -> float:
        return self.qty if self.side is Side.BUY else -self.qty

    @property
    def slippage_cost(self) -> float:
        """Cash lost to spread and slippage on this fill alone."""
        if not self.reference_price:
            return 0.0
        return abs(self.price - self.reference_price) * self.qty


@dataclass
class Trade:
    """A completed round trip, from flat back to flat.

    Three profit figures, which is two more than most backtesters report:

    * ``gross_pnl`` - the market move, measured mid to mid. What the signal was worth.
    * ``executed_pnl`` - the same trade at the prices you were really filled at.
    * ``net_pnl`` - after commission too. The only one that reaches your account.
    """

    entry_ts: int
    exit_ts: int
    side: Side
    qty: float
    entry_price: float
    exit_price: float
    fees: float
    entry_reference: float = 0.0
    exit_reference: float = 0.0
    reason: str = ""

    @property
    def _direction(self) -> float:
        return 1.0 if self.side is Side.BUY else -1.0

    @property
    def gross_pnl(self) -> float:
        entry = self.entry_reference or self.entry_price
        exit_ = self.exit_reference or self.exit_price
        return self._direction * (exit_ - entry) * self.qty

    @property
    def executed_pnl(self) -> float:
        return self._direction * (self.exit_price - self.entry_price) * self.qty

    @property
    def slippage_cost(self) -> float:
        return self.gross_pnl - self.executed_pnl

    @property
    def net_pnl(self) -> float:
        return self.executed_pnl - self.fees

    @property
    def total_cost(self) -> float:
        return self.slippage_cost + self.fees

    @property
    def return_pct(self) -> float:
        cost_basis = self.entry_price * self.qty
        return self.net_pnl / cost_basis if cost_basis else 0.0


@dataclass
class EquityPoint:
    ts: int
    equity: float
    price: float
    position: float
    cash: float
