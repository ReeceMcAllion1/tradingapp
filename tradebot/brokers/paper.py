"""Simulated execution against the cost model.

Used by backtests and by paper trading. Paper trading is the same broker as the
backtest, pointed at live market data - so the only thing that changes when you go
live is that the fills stop being imaginary.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..costs import CostModel
from ..types import Fill, Liquidity, Side
from .base import Broker


@dataclass
class PaperBroker(Broker):
    costs: CostModel
    is_live: bool = False

    def execute(self, ts: int, signed_qty: float, reference_price: float, reason: str,
                liquidity: Liquidity = Liquidity.TAKER) -> Fill | None:
        if abs(signed_qty) < 1e-12:
            return None
        side = Side.BUY if signed_qty > 0 else Side.SELL
        # A resting order is filled at the price it named, by definition - it did not
        # cross the spread, so it does not pay the spread. Charging half-spread and
        # slippage on a maker fill would model an order that crossed the book, which is
        # the opposite of what happened.
        price = reference_price if liquidity is Liquidity.MAKER else self.costs.fill_price(side, reference_price)
        qty = abs(signed_qty)
        return Fill(
            ts=ts,
            side=side,
            qty=qty,
            price=price,
            fee=self.costs.fee(qty * price, liquidity),
            reference_price=reference_price,
            liquidity=liquidity,
            reason=reason,
        )
