"""Market data from Alpaca (alpaca.markets).

Handles two markets from one class, chosen by ``asset_class``:

* ``"crypto"`` - symbols like ``BTC/USD``, bars from the ``v1beta3/crypto/us``
  endpoint, available 24/7.
* ``"us_equity"`` - symbols like ``AAPL``, bars from the ``v2/stocks`` endpoint,
  only printed during US market hours. Outside those hours the stream simply has
  no new bar to hand over, which is correct: nothing traded.

Alpaca's crypto data is public, but the equities endpoint needs credentials, so
this feed sends ``APCA-API-KEY-ID`` / ``APCA-API-SECRET-KEY`` whenever they are in
the environment. They are never read from a config file.

Like every feed here, the bar currently forming is discarded - trading on a close
price that is still moving is a way to test on data you would not have had.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator

from ..types import Candle
from .base import Feed, validate_series
from .cryptocom import FeedError

CRYPTO_BARS_URL = "https://data.alpaca.markets/v1beta3/crypto/us/bars"
STOCK_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
MAX_PER_REQUEST = 10_000

# The repo's interval vocabulary mapped to Alpaca's timeframe strings.
TIMEFRAME = {
    "1m": "1Min",
    "5m": "5Min",
    "15m": "15Min",
    "30m": "30Min",
    "1h": "1Hour",
    "2h": "2Hour",
    "4h": "4Hour",
    "12h": "12Hour",
    "1D": "1Day",
    "7D": "1Week",
}

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "12h": 43_200_000,
    "1D": 86_400_000,
    "7D": 604_800_000,
}


def _parse_ts(value: str) -> int:
    """RFC3339 string -> epoch milliseconds. Alpaca stamps every bar at its open."""
    text = value.replace("Z", "+00:00")
    return int(dt.datetime.fromisoformat(text).timestamp() * 1000)


def _to_candle(row: dict) -> Candle:
    return Candle(
        ts=_parse_ts(row["t"]),
        open=float(row["o"]),
        high=float(row["h"]),
        low=float(row["l"]),
        close=float(row["c"]),
        volume=float(row.get("v", 0.0)),
    )


def _headers() -> dict:
    key = os.environ.get("APCA_API_KEY_ID", "")
    secret = os.environ.get("APCA_API_SECRET_KEY", "")
    headers = {"User-Agent": "tradebot/1.0", "Accept": "application/json"}
    if key and secret:
        headers["APCA-API-KEY-ID"] = key
        headers["APCA-API-SECRET-KEY"] = secret
    return headers


def _get(url: str, timeout: float, retries: int = 4) -> dict:
    """GET with exponential backoff. Alpaca rate-limits with HTTP 429 rather than failing."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=_headers())
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            last = FeedError(f"HTTP {exc.code} from Alpaca: {detail}")
            # 4xx other than 429 will not fix themselves on a retry.
            if exc.code != 429 and 400 <= exc.code < 500:
                raise last from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
        if attempt < retries - 1:
            time.sleep(2**attempt)
    raise FeedError(f"failed to fetch {url}: {last}") from last


class AlpacaFeed(Feed):
    def __init__(
        self,
        symbol: str = "BTC/USD",
        interval: str = "1h",
        asset_class: str = "crypto",
        data_feed: str = "iex",
        timeout: float = 20.0,
        poll_seconds: float | None = None,
    ) -> None:
        if interval not in TIMEFRAME:
            raise ValueError(
                f"unsupported interval {interval!r}; try one of {', '.join(TIMEFRAME)}"
            )
        if asset_class not in ("crypto", "us_equity"):
            raise ValueError("asset_class must be 'crypto' or 'us_equity'")
        self.symbol = symbol
        self.interval = interval
        self.asset_class = asset_class
        self.data_feed = data_feed
        self.timeout = timeout
        self.poll_seconds = poll_seconds

    @property
    def interval_ms(self) -> int:
        return INTERVAL_MS[self.interval]

    # ------------------------------------------------------------------ requests

    def _url(self, limit: int, start_ms: int | None, page_token: str | None) -> str:
        params = {
            "symbols": self.symbol,
            "timeframe": TIMEFRAME[self.interval],
            "limit": min(limit, MAX_PER_REQUEST),
            "sort": "asc",
        }
        if self.asset_class == "us_equity":
            params["feed"] = self.data_feed
        if start_ms is not None:
            params["start"] = dt.datetime.fromtimestamp(
                start_ms / 1000.0, tz=dt.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        if page_token:
            params["page_token"] = page_token
        base = CRYPTO_BARS_URL if self.asset_class == "crypto" else STOCK_BARS_URL
        return f"{base}?{urllib.parse.urlencode(params)}"

    def _bars_from(self, payload: dict) -> list[Candle]:
        rows = (payload.get("bars") or {}).get(self.symbol) or []
        return [_to_candle(row) for row in rows]

    # ------------------------------------------------------------------ Feed API

    def history(self, limit: int = 1000) -> list[Candle]:
        """Fetch ``limit`` recent bars, paging forward from an estimated start."""
        # Ask for a window a bit wider than needed - weekends and market closures
        # mean equities print far fewer bars than wall-clock time would suggest.
        span = self.interval_ms * limit
        pad = 3 if self.asset_class == "us_equity" else 1
        start_ms = int(time.time() * 1000) - span * pad

        collected: dict[int, Candle] = {}
        page_token: str | None = None
        while len(collected) < limit + MAX_PER_REQUEST:
            payload = _get(self._url(MAX_PER_REQUEST, start_ms, page_token), self.timeout)
            page = self._bars_from(payload)
            for candle in page:
                collected[candle.ts] = candle
            page_token = payload.get("next_page_token")
            if not page_token:
                break

        if not collected:
            raise FeedError(f"no bars returned for {self.symbol} {self.interval}")
        return validate_series(list(collected.values()))[-limit:]

    def latest_closed(self) -> Candle:
        """The most recent bar whose interval has fully elapsed.

        The lookback has to clear the longest gap with no prints. For crypto a few
        intervals is plenty; for equities a Monday-morning poll must still reach the
        previous Friday's close, so the window is at least four days.
        """
        lookback = self.interval_ms * 5
        if self.asset_class == "us_equity":
            lookback = max(lookback, 4 * 86_400_000)
        start_ms = int(time.time() * 1000) - lookback
        payload = _get(self._url(10, start_ms, None), self.timeout)
        page = self._bars_from(payload)
        if not page:
            raise FeedError(f"no bars returned for {self.symbol}")
        ordered = validate_series(page)
        now_ms = int(time.time() * 1000)
        closed = [c for c in ordered if c.ts + self.interval_ms <= now_ms]
        if not closed:
            return ordered[-2] if len(ordered) > 1 else ordered[-1]
        return closed[-1]

    def stream(self) -> Iterator[Candle]:
        """Yield each bar once, as soon as it has closed."""
        interval_s = self.interval_ms / 1000.0
        wait = self.poll_seconds if self.poll_seconds is not None else max(5.0, interval_s / 10.0)
        last_ts = 0
        while True:
            try:
                candle = self.latest_closed()
                if candle.ts > last_ts:
                    last_ts = candle.ts
                    yield candle
            except FeedError:
                # A dropped request must not kill an unattended run; the next poll
                # picks up whatever closed in the meantime.
                pass
            time.sleep(wait)
