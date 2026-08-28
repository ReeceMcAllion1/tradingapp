"""Cash, position and profit-and-loss accounting.

The portfolio is deliberately dumb: it takes fills and tells you where you stand.
It never decides anything. That separation is what lets the backtester and the
live runner share exactly the same accounting code, so a paper run and a live run
cannot silently disagree about what you own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .types import EquityPoint, Fill, Side, Trade


@dataclass
class Portfolio:
    """Tracks one instrument against a cash balance.

    Positions are signed: positive is long, negative is short. Shorting is modelled
    without a margin requirement or borrow cost, which flatters short-heavy
    strategies - keep ``allow_short`` off unless you know your venue's terms.
    """

    starting_cash: float
    cash: float = field(init=False)
    qty: float = 0.0
    avg_price: float = 0.0

    fees_paid: float = 0.0
    realised_gross: float = 0.0
    slippage_paid: float = 0.0

    trades: list[Trade] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)

    _open_ts: int = 0
    _open_fees: float = 0.0
    _open_reference: float = 0.0

    def __post_init__(self) -> None:
        if self.starting_cash <= 0:
            raise ValueError("starting_cash must be positive")
        self.cash = self.starting_cash

    # ------------------------------------------------------------------ state

    @property
    def is_flat(self) -> bool:
        return abs(self.qty) < 1e-12

    @property
    def is_long(self) -> bool:
        return self.qty > 1e-12

    def equity(self, price: float) -> float:
        """Cash plus the mark-to-market value of the open position."""
        return self.cash + self.qty * price

    def exposure(self, price: float) -> float:
        """Current position as a fraction of equity, matching ``Decision.target_weight``."""
        equity = self.equity(price)
        if equity <= 0:
            return 0.0
        return (self.qty * price) / equity

    def unrealised(self, price: float) -> float:
        return (price - self.avg_price) * self.qty if not self.is_flat else 0.0

    @property
    def realised_net(self) -> float:
        return self.realised_gross - self.slippage_paid - self.fees_paid

    @property
    def total_costs(self) -> float:
        """Everything the venue took: spread, slippage and commission."""
        return self.slippage_paid + self.fees_paid

    # ------------------------------------------------------------------ mutation

    def apply(self, fill: Fill) -> list[Trade]:
        """Book a fill, returning any round trips it completed.

        The fill is allocated in two passes: first it closes as much of an opposing
        position as it can, then any remainder opens or extends a position. A fill
        that flips long to short therefore does both, and the fee is split between
        the two parts in proportion to quantity.
        """
        if fill.qty <= 0:
            raise ValueError("fill quantity must be positive")

        self.cash -= fill.signed_qty * fill.price
        self.cash -= fill.fee
        self.fees_paid += fill.fee
        self.fills.append(fill)

        remaining = fill.qty
        closed: list[Trade] = []

        opposes = not self.is_flat and (fill.signed_qty > 0) != (self.qty > 0)
        if opposes:
            held = abs(self.qty)
            closing = min(remaining, held)
            entry_fees = self._open_fees * (closing / held)
            exit_fees = fill.fee * (closing / fill.qty)

            trade = Trade(
                entry_ts=self._open_ts,
                exit_ts=fill.ts,
                side=Side.BUY if self.qty > 0 else Side.SELL,
                qty=closing,
                entry_price=self.avg_price,
                exit_price=fill.price,
                fees=entry_fees + exit_fees,
                entry_reference=self._open_reference,
                exit_reference=fill.reference_price or fill.price,
                reason=fill.reason,
            )
            self.realised_gross += trade.gross_pnl
            self.slippage_paid += trade.slippage_cost
            closed.append(trade)

            self._open_fees -= entry_fees
            self.qty += closing if self.qty < 0 else -closing
            remaining -= closing
            if abs(self.qty) < 1e-12:
                self.qty = 0.0
                self.avg_price = 0.0
                self._open_reference = 0.0
                self._open_fees = 0.0

        if remaining > 1e-12:
            fee_share = fill.fee * (remaining / fill.qty)
            reference = fill.reference_price or fill.price
            if self.is_flat:
                self.qty = remaining if fill.signed_qty > 0 else -remaining
                self.avg_price = fill.price
                self._open_reference = reference
                self._open_ts = fill.ts
                self._open_fees = fee_share
            else:
                total = abs(self.qty) + remaining
                held = abs(self.qty)
                self.avg_price = (self.avg_price * held + fill.price * remaining) / total
                self._open_reference = (self._open_reference * held + reference * remaining) / total
                self.qty = total if self.qty > 0 else -total
                self._open_fees += fee_share

        self.trades.extend(closed)
        return closed

    def mark(self, ts: int, price: float) -> EquityPoint:
        point = EquityPoint(
            ts=ts,
            equity=self.equity(price),
            price=price,
            position=self.qty,
            cash=self.cash,
        )
        self.equity_curve.append(point)
        return point

    def state(self) -> dict:
        """Serialisable snapshot, used to resume a live run after a restart."""
        return {
            "cash": self.cash,
            "qty": self.qty,
            "avg_price": self.avg_price,
            "fees_paid": self.fees_paid,
            "realised_gross": self.realised_gross,
            "slippage_paid": self.slippage_paid,
            "open_ts": self._open_ts,
            "open_fees": self._open_fees,
            "open_reference": self._open_reference,
        }

    def restore(self, state: dict) -> None:
        self.cash = float(state["cash"])
        self.qty = float(state["qty"])
        self.avg_price = float(state["avg_price"])
        self.fees_paid = float(state.get("fees_paid", 0.0))
        self.realised_gross = float(state.get("realised_gross", 0.0))
        self.slippage_paid = float(state.get("slippage_paid", 0.0))
        self._open_ts = int(state.get("open_ts", 0))
        self._open_fees = float(state.get("open_fees", 0.0))
        self._open_reference = float(state.get("open_reference", 0.0))
