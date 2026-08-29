"""Live execution against a Crypto.com Exchange account. Real money.

Everything in this file spends your actual balance, so it is wrapped in four
independent safety gates. All four must be open before a single order can leave your
machine:

1. ``live.enabled = true`` in ``config.toml``.
2. ``CRYPTOCOM_API_KEY`` and ``CRYPTOCOM_API_SECRET`` present in the environment.
   Keys are never read from the config file, so you cannot commit them by accident.
3. ``dry_run = false``. While ``dry_run`` is true - the default - orders are logged
   in full but not sent.
4. ``--yes-really-trade-live`` passed on the command line, every run.

Client-side ``max_order_notional`` caps the size of any single order regardless of
what the strategy asked for. It is the last line of defence against a bug in your own
code, and it is enforced here rather than upstream so nothing can route around it.

Before enabling any of this, run ``python -m tradebot verify-keys``. It makes one
read-only balance call and places no orders. Set your API key to trade-only
permissions with withdrawals disabled and an IP allowlist.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from ..costs import CostModel
from ..types import Fill, Liquidity, Side
from .base import Broker, BrokerError

BASE_URL = "https://api.crypto.com/exchange/v1"
log = logging.getLogger("tradebot.broker")


def floor_to_decimals(qty: float, decimals: int) -> float:
    """Round a quantity down to the venue's precision, never up.

    ``round()`` goes to nearest, which means it can round *up* - and every use of this
    number is a ceiling of some kind. Rounding a size up overshoots what the strategy
    asked for; rounding the ``max_order_notional`` trim up overshoots the cap that
    exists to contain a sizing bug. A limit that can be exceeded by rounding is not a
    limit. On a coarse lot size the gap is not academic: trimming to a whole unit at
    £30 a unit turns a £50 cap into a £60 order.
    """
    if decimals < 0:
        raise ValueError("decimals must not be negative")
    factor = 10**decimals
    return math.floor(abs(qty) * factor) / factor


def params_to_str(obj: object, level: int = 0, max_level: int = 3) -> str:
    """Crypto.com's canonical parameter serialisation for request signing.

    Keys are sorted, nested objects are flattened, and booleans lower-cased. The
    signature will not match if any of that differs, so this mirrors the published
    reference implementation exactly.

    Floats are refused outright. Both sides of the signature have to serialise every
    value identically, and a float is the one type where they reliably disagree:
    Python renders 0.00001 as ``1e-05`` while the exchange, parsing the JSON number
    and re-serialising it to verify, writes ``0.00001``. The signature then fails to
    match, and it does so *only* for small or awkward quantities - so it passes every
    test with round numbers and rejects the real order. The venue's own documentation
    says to send numbers as strings for exactly this reason.

    Refusing here turns that into a loud local failure at the call site rather than an
    opaque 401 from the exchange at the worst possible moment. It is raised as a plain
    ValueError; the broker converts it into a BrokerError, which the engine already
    treats as a rejection and halts on after a few in a row.
    """
    if isinstance(obj, float):
        raise ValueError(
            f"cannot sign the float {obj!r}: Crypto.com requires numbers as strings, "
            f'because "1e-05" and "0.00001" hash differently. Format it yourself, '
            f'e.g. f"{{value:.8f}}".'
        )
    if level >= max_level or not isinstance(obj, dict):
        return str(obj)
    out = ""
    for key in sorted(obj):
        value = obj[key]
        out += key
        if value is None:
            out += "null"
        elif isinstance(value, bool):
            out += str(value).lower()
        elif isinstance(value, float):
            raise ValueError(
                f"cannot sign the float {value!r} under key {key!r}: Crypto.com requires "
                f'numbers as strings, because "1e-05" and "0.00001" hash differently. '
                f'Format it yourself, e.g. f"{{value:.8f}}".'
            )
        elif isinstance(value, list):
            for item in value:
                out += params_to_str(item, level + 1, max_level)
        else:
            out += str(value)
    return out


@dataclass
class CryptoComBroker(Broker):
    """Places real market orders. Read the module docstring before using."""

    symbol: str
    costs: CostModel
    enabled: bool = False
    dry_run: bool = True
    max_order_notional: float = 50.0
    qty_decimals: int = 6
    timeout: float = 20.0
    is_live: bool = True

    _request_id: int = field(default=1, init=False)

    # ------------------------------------------------------------------ auth

    @property
    def api_key(self) -> str:
        return os.environ.get("CRYPTOCOM_API_KEY", "")

    @property
    def api_secret(self) -> str:
        return os.environ.get("CRYPTOCOM_API_SECRET", "")

    def _require_credentials(self) -> None:
        if not self.api_key or not self.api_secret:
            raise BrokerError(
                "CRYPTOCOM_API_KEY and CRYPTOCOM_API_SECRET must be set in the environment. "
                "Never put them in config.toml."
            )

    def _signed_request(self, method: str, params: dict) -> dict:
        self._require_credentials()
        self._request_id += 1
        nonce = int(time.time() * 1000)
        payload = {
            "id": self._request_id,
            "method": method,
            "api_key": self.api_key,
            "params": params,
            "nonce": nonce,
        }
        try:
            encoded_params = params_to_str(params)
        except ValueError as exc:
            raise BrokerError(f"{method} cannot be signed: {exc}") from exc
        signature_base = f"{method}{self._request_id}{self.api_key}{encoded_params}{nonce}"
        payload["sig"] = hmac.new(
            self.api_secret.encode("utf-8"),
            signature_base.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        request = urllib.request.Request(
            f"{BASE_URL}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "tradebot/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise BrokerError(f"{method} failed with HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise BrokerError(f"{method} could not reach the exchange: {exc}") from exc

        if body.get("code") not in (0, None):
            raise BrokerError(f"{method} rejected: code {body.get('code')} {body.get('message', '')}")
        return body.get("result", {})

    # ------------------------------------------------------------------ read-only

    def verify(self) -> str:
        """Read-only balance check. Places no orders and needs no gates open."""
        result = self._signed_request("private/user-balance", {})
        accounts = result.get("data") or []
        if not accounts:
            return "credentials work, but the account reported no balances"
        summary = accounts[0]
        total = summary.get("total_available_balance", "unknown")
        currency = summary.get("instrument_name", "USD")
        return f"credentials work - available balance {total} {currency}"

    def sync_position(self) -> float | None:
        try:
            result = self._signed_request("private/get-positions", {"instrument_name": self.symbol})
        except BrokerError as exc:
            log.warning("could not sync position from exchange: %s", exc)
            return None
        for row in result.get("data") or []:
            if row.get("instrument_name") == self.symbol:
                return float(row.get("quantity", 0.0))
        return 0.0

    # ------------------------------------------------------------------ orders

    def execute(self, ts: int, signed_qty: float, reference_price: float, reason: str,
                liquidity: Liquidity = Liquidity.TAKER) -> Fill | None:
        """Places a MARKET order regardless of ``liquidity``.

        A market order is a taker by definition, so the flag is accepted and ignored
        rather than quietly booking a maker fee this venue would not have charged.
        Resting limit orders live-side are not implemented; a backtest run with
        ``maker_offset_bps`` set therefore does not describe what this broker does.
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

        params = {
            "instrument_name": self.symbol,
            "side": "BUY" if side is Side.BUY else "SELL",
            "type": "MARKET",
            "quantity": f"{qty:.{self.qty_decimals}f}",
        }

        if not self.enabled or self.dry_run:
            state = "live.enabled is false" if not self.enabled else "dry_run is true"
            log.warning("WOULD SEND (%s): %s  [%s]", state, params, reason)
            return None

        log.warning("SENDING LIVE ORDER: %s  [%s]", params, reason)
        result = self._signed_request("private/create-order", params)
        order_id = result.get("order_id", "unknown")

        # A market order's real average price is only known once it has filled. The
        # reference price plus the cost model is an estimate; reconcile_position() is
        # what corrects the books against the exchange's own record.
        price = self.costs.fill_price(side, reference_price)
        log.warning("order %s accepted: %s %.8f %s ~%.2f", order_id, params["side"], qty, self.symbol, price)
        return Fill(
            ts=ts,
            side=side,
            qty=qty,
            price=price,
            fee=self.costs.fee(qty * price, Liquidity.TAKER),
            reference_price=reference_price,
            liquidity=Liquidity.TAKER,
            reason=f"{reason} (order {order_id})",
        )
