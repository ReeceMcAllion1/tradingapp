"""Order execution.

The engine never talks to an exchange directly. It hands a signed quantity to a
broker and gets back a ``Fill`` - or ``None`` if the order could not be placed. That
indirection is what makes a paper run and a live run the same code path, with the
only difference being which broker object was constructed at startup.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..types import Fill


class BrokerError(RuntimeError):
    pass


class Broker(ABC):
    """Turns an intended position change into a fill."""

    #: Whether this broker risks real money. Used for the confirmations in the CLI.
    is_live: bool = False

    @abstractmethod
    def execute(self, ts: int, signed_qty: float, reference_price: float, reason: str) -> Fill | None:
        """Buy (positive qty) or sell (negative qty). Returns the resulting fill."""

    def sync_position(self) -> float | None:
        """Real position held at the venue, if the broker can tell. ``None`` if not.

        A live run should trust the exchange over its own memory: fills can happen
        that the bot did not initiate, and a stale local position is how a bot ends up
        selling something it does not own.
        """
        return None

    def verify(self) -> str:
        """Prove credentials work with a read-only call. Returns a human-readable summary."""
        return "no verification needed for this broker"
