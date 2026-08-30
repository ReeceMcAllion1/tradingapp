"""Execution against an Alpaca brokerage account.

One class covers both of Alpaca's endpoints:

* ``paper = True`` -> ``https://paper-api.alpaca.markets``. Orders are real and the
  position is tracked by Alpaca, but the money is not. This is the default and it
  does **not** count as live for the real-money command gates.
* ``paper = False`` -> ``https://api.alpaca.markets``. Real money. It obeys the same
  gates as the Crypto.com broker: ``live.enabled``, ``dry_run = false`` and the
  ``--yes-really-trade-live`` flag on every run.

Credentials come from the environment only - ``APCA_API_KEY_ID`` and
``APCA_API_SECRET_KEY`` - so a config file can be committed without leaking them.

Alpaca does not itemise commission on a fill. Equities are commission-free; crypto
charges a spread-based fee. The fill *price* used here is Alpaca's real
``filled_avg_price``; the fee is estimated from the configured ``CostModel`` so the
reports still separate "the market moved" from "the venue took a cut". Set the cost
model to your account's real crypto fee tier.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from ..costs import CostModel
from ..types import Fill, Liquidity, Side
from .base import Broker, BrokerError
from .cryptocom import floor_to_decimals

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"
log = logging.getLogger("tradebot.broker")


@dataclass
class AlpacaBroker(Broker):
    """Places market orders through Alpaca. Read the module docstring first."""

    symbol: str
    costs: CostModel
    asset_class: str = "crypto"
    paper: bool = True
    enabled: bool = False
    dry_run: bool = True
    max_order_notional: float = 50.0
    qty_decimals: int = 6
    timeout: float = 20.0
    fill_wait_seconds: float = 10.0

    _order_seq: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.asset_class not in ("crypto", "us_equity"):
            raise ValueError("asset_class must be 'crypto' or 'us_equity'")

    # ------------------------------------------------------------------ identity

    @property
    def is_live(self) -> bool:
        """Only real-money trading is 'live'. Paper is real orders, imaginary money."""
        return not self.paper

    @property
    def base_url(self) -> str:
        return PAPER_URL if self.paper else LIVE_URL

    # ------------------------------------------------------------------ auth / http

    @property
    def api_key(self) -> str:
        return os.environ.get("APCA_API_KEY_ID", "")

    @property
    def api_secret(self) -> str:
        return os.environ.get("APCA_API_SECRET_KEY", "")

    def _require_credentials(self) -> None:
        if not self.api_key or not self.api_secret:
            raise BrokerError(
                "APCA_API_KEY_ID and APCA_API_SECRET_KEY must be set in the environment. "
                "Never put them in a config file."
            )

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        self._require_credentials()
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.api_secret,
                "Content-Type": "application/json",
                "User-Agent": "tradebot/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise BrokerError(f"{method} {path} failed with HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise BrokerError(f"{method} {path} could not reach Alpaca: {exc}") from exc
        return json.loads(raw) if raw else {}

    # ------------------------------------------------------------------ read-only

    def verify(self) -> str:
        """Read-only account check. Places no orders."""
        account = self._request("GET", "/v2/account")
        status = account.get("status", "unknown")
        cash = account.get("cash", "unknown")
        currency = account.get("currency", "USD")
        kind = "paper" if self.paper else "LIVE"
        return f"credentials work - {kind} account {status}, cash {cash} {currency}"

    def sync_position(self) -> float | None:
        """Signed quantity Alpaca currently holds for this symbol. 0.0 if flat."""
        try:
            rows = self._request("GET", "/v2/positions")
        except BrokerError as exc:
            log.warning("could not sync position from Alpaca: %s", exc)
            return None
        wanted = {self.symbol, self.symbol.replace("/", "")}
        for row in rows if isinstance(rows, list) else []:
            if row.get("symbol") in wanted:
                qty = float(row.get("qty", 0.0))
                return qty if row.get("side") != "short" else -qty
        return 0.0

    # ------------------------------------------------------------------ orders

    def execute(self, ts: int, signed_qty: float, reference_price: float, reason: str,
                liquidity: Liquidity = Liquidity.TAKER) -> Fill | None:
        """Submit a MARKET order and return the fill Alpaca reports.

        ``liquidity`` is accepted and ignored: a market order is always a taker, and
        booking a maker fee for one this venue would not have charged would understate
        cost. A backtest run with ``maker_offset_bps`` set does not describe this path.
        """
        if abs(signed_qty) < 1e-12:
            return None

        side = Side.BUY if signed_qty > 0 else Side.SELL
        qty = floor_to_decimals(abs(signed_qty), self.qty_decimals)
        if qty <= 0:
            log.info("order of %.10f rounds to zero at %d dp, skipping", signed_qty, self.qty_decimals)
            return None

        notional = qty * reference_price
        if notional > self.max_order_notional:
            capped = self.max_order_notional / reference_price
            log.warning(
                "order notional %.2f exceeds max_order_notional %.2f - trimming %.8f to %.8f",
                notional, self.max_order_notional, qty, capped,
            )
            qty = floor_to_decimals(capped, self.qty_decimals)
            if qty <= 0:
                return None

        order = {
            "symbol": self.symbol,
            "qty": f"{qty:.{self.qty_decimals}f}",
            "side": side.value,
            "type": "market",
            "time_in_force": "gtc" if self.asset_class == "crypto" else "day",
        }

        # The paper endpoint is not real money, so it is exempt from the dry_run and
        # enabled gates - those exist to stop real orders. Real trading (paper=False)
        # honours them exactly like the Crypto.com broker.
        if not self.paper and (not self.enabled or self.dry_run):
            state = "live.enabled is false" if not self.enabled else "dry_run is true"
            log.warning("WOULD SEND (%s): %s  [%s]", state, order, reason)
            return None

        banner = "SENDING LIVE ORDER" if not self.paper else "sending paper order"
        log.warning("%s: %s  [%s]", banner, order, reason)
        placed = self._request("POST", "/v2/orders", order)
        order_id = placed.get("id", "unknown")
        filled = self._await_fill(order_id, fallback_status=placed.get("status", ""))

        fill_qty = float(filled.get("filled_qty") or 0.0)
        avg_price = float(filled.get("filled_avg_price") or 0.0)
        if fill_qty <= 0 or avg_price <= 0:
            raise BrokerError(
                f"order {order_id} did not fill within {self.fill_wait_seconds:.0f}s "
                f"(status {filled.get('status', 'unknown')})"
            )

        log.warning("order %s filled: %s %.8f %s @ %.2f", order_id, side.value, fill_qty, self.symbol, avg_price)
        return Fill(
            ts=ts,
            side=side,
            qty=fill_qty,
            price=avg_price,
            fee=self.costs.fee(fill_qty * avg_price, Liquidity.TAKER),
            reference_price=reference_price,
            liquidity=Liquidity.TAKER,
            reason=f"{reason} (order {order_id})",
        )

    def _await_fill(self, order_id: str, fallback_status: str = "") -> dict:
        """Poll the order until it fills or the wait budget runs out.

        A market order on a liquid symbol fills in well under a second, but the POST
        response is often returned before the fill is booked, with
        ``filled_avg_price`` still null. Without this poll every fill would look like
        a non-fill and the engine would treat a placed order as a rejection.
        """
        deadline = time.time() + self.fill_wait_seconds
        last: dict = {"status": fallback_status}
        while time.time() < deadline:
            try:
                last = self._request("GET", f"/v2/orders/{order_id}")
            except BrokerError as exc:
                log.warning("could not read order %s: %s", order_id, exc)
                break
            if last.get("status") in ("filled", "partially_filled") and last.get("filled_avg_price"):
                return last
            if last.get("status") in ("canceled", "expired", "rejected", "done_for_day"):
                return last
            time.sleep(0.5)
        return last
