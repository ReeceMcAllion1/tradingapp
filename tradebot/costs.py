"""Transaction cost model.

This is the most important module in the project. A backtest that ignores costs
will make almost any strategy look profitable, and the shorter the holding period
the bigger the lie. Every simulated fill goes through here so that the numbers you
see are the numbers you would actually keep.

Three separate costs are charged on every fill:

* **Half-spread** - you buy at the ask and sell at the bid, so you pay half the
  quoted spread on entry and half again on exit.
* **Slippage** - the price moves between deciding and being filled, and a market
  order eats into the book. Always modelled against you.
* **Commission** - the venue's fee, charged on notional.

The round-trip cost of a position is therefore roughly
``2 * (half_spread_bps + slippage_bps + fee_bps)``. With the conservative retail
defaults below that is 28 bps, or £2.80 on a £1,000 position - which is why a
strategy that aims to bank "a few pence" per trade cannot work. Run
``python -m tradebot demo`` to see that arithmetic play out on real price data.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import Liquidity, Side

BPS = 1e-4


@dataclass(frozen=True)
class CostModel:
    """Costs in basis points (1 bp = 0.01%).

    Defaults are deliberately pessimistic and roughly reflect a retail crypto
    account trading a liquid pair. Override them in ``config.toml`` with your
    venue's real numbers - and if in doubt, guess worse than you think.
    """

    taker_fee_bps: float = 10.0
    maker_fee_bps: float = 5.0
    half_spread_bps: float = 2.0
    slippage_bps: float = 2.0
    flat_fee: float = 0.0

    def __post_init__(self) -> None:
        for name in ("taker_fee_bps", "maker_fee_bps", "half_spread_bps", "slippage_bps", "flat_fee"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must not be negative")

    def fill_price(self, side: Side, reference_price: float) -> float:
        """Price actually paid or received, moved against you from the reference."""
        adverse = (self.half_spread_bps + self.slippage_bps) * BPS
        if side is Side.BUY:
            return reference_price * (1.0 + adverse)
        return reference_price * (1.0 - adverse)

    def fee(self, notional: float, liquidity: Liquidity = Liquidity.TAKER) -> float:
        rate = self.taker_fee_bps if liquidity is Liquidity.TAKER else self.maker_fee_bps
        return abs(notional) * rate * BPS + self.flat_fee

    @property
    def round_trip_bps(self) -> float:
        """Proportional cost of opening and closing one position, in basis points.

        This excludes ``flat_fee``, which is not proportional to anything - use
        ``breakeven_cash`` or ``breakeven_move_pct(notional)`` when a flat fee applies.
        """
        return 2.0 * (self.half_spread_bps + self.slippage_bps + self.taker_fee_bps)

    def breakeven_move_pct(self, notional: float | None = None) -> float:
        """How far price must move in your favour just to break even, as a percent.

        With a flat commission this depends on position size, and brutally so for small
        accounts: a £6 round trip is 0.6% of a £1,000 position but 6% of a £100 one.
        Pass ``notional`` whenever you know it.
        """
        proportional = self.round_trip_bps * BPS * 100.0
        if notional is None or self.flat_fee <= 0 or notional <= 0:
            return proportional
        return self.breakeven_cash(notional) / abs(notional) * 100.0

    def breakeven_cash(self, notional: float) -> float:
        """Cash a round trip on ``notional`` costs you before any profit exists."""
        return abs(notional) * self.round_trip_bps * BPS + 2.0 * self.flat_fee
