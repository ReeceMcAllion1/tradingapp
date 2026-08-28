"""Public market data from the Crypto.com Exchange.

Read-only and unauthenticated - this module never needs an API key and never places
an order. It is used both to download history for backtests and to drive the live
loop.

The exchange caps each request at 300 bars, so ``history`` pages backwards through
``end_ts`` until it has what you asked for.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator

from ..types import Candle
from .base import Feed, validate_series

BASE_URL = "https://api.crypto.com/exchange/v1"
MAX_PER_REQUEST = 300

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


class FeedError(RuntimeError):
    pass


def _get(url: str, timeout: float, retries: int = 4) -> dict:
    """GET with exponential backoff. Market data endpoints rate-limit rather than fail."""
    last: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "tradebot/1.0"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("code") not in (0, None):
                raise FeedError(f"exchange returned code {payload.get('code')}: {payload}")
            return payload
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, FeedError) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(2**attempt)
    raise FeedError(f"failed to fetch {url}: {last}") from last


def _to_candle(row: dict) -> Candle:
    return Candle(
        ts=int(row["t"]),
        open=float(row["o"]),
        high=float(row["h"]),
        low=float(row["l"]),
        close=float(row["c"]),
        volume=float(row.get("v", 0.0)),
    )


class CryptoComFeed(Feed):
    def __init__(
        self,
        symbol: str = "BTC_USD",
        interval: str = "5m",
        timeout: float = 20.0,
        poll_seconds: float | None = None,
    ) -> None:
        if interval not in INTERVAL_MS:
            raise ValueError(f"unsupported interval {interval!r}; try one of {', '.join(INTERVAL_MS)}")
        self.symbol = symbol
        self.interval = interval
        self.timeout = timeout
        self.poll_seconds = poll_seconds

    @property
    def interval_ms(self) -> int:
        return INTERVAL_MS[self.interval]

    def _page(self, count: int, end_ts: int | None = None) -> list[Candle]:
        url = (
            f"{BASE_URL}/public/get-candlestick?instrument_name={self.symbol}"
            f"&timeframe={self.interval}&count={min(count, MAX_PER_REQUEST)}"
        )
        if end_ts is not None:
            url += f"&end_ts={end_ts}"
        payload = _get(url, self.timeout)
        rows = payload.get("result", {}).get("data") or []
        if not rows:
            return []
        return [_to_candle(row) for row in rows]

    def history(self, limit: int = 1000) -> list[Candle]:
        """Fetch ``limit`` recent bars, paging backwards as needed."""
        collected: dict[int, Candle] = {}
        end_ts: int | None = None

        while len(collected) < limit:
            page = self._page(MAX_PER_REQUEST, end_ts)
            if not page:
                break
            before = len(collected)
            for candle in page:
                collected[candle.ts] = candle
            if len(collected) == before:
                break  # the exchange is repeating itself; nothing older is available
            end_ts = min(page, key=lambda c: c.ts).ts - 1

        if not collected:
            raise FeedError(f"no candles returned for {self.symbol} {self.interval}")
        return validate_series(list(collected.values()))[-limit:]

    def latest_closed(self) -> Candle:
        """The most recent *closed* bar.

        The exchange includes the bar currently forming, whose close price is still
        moving. Trading on it would mean acting on a price that has not settled, so it
        is always discarded.
        """
        page = self._page(3)
        if not page:
            raise FeedError(f"no candles returned for {self.symbol}")
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
                # A dropped connection must not kill an unattended run; the next poll
                # picks up any bar that closed in the meantime.
                pass
            time.sleep(wait)
