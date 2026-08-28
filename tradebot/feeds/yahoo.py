"""Daily stock and ETF bars from Yahoo Finance.

Free and keyless, which is why it is here, but it is an undocumented endpoint that
Yahoo can change or rate-limit at any time. Download once to CSV and backtest against
the file rather than hitting it repeatedly.

**On price adjustment**, which decides whether a long backtest means anything:

* Yahoo's ``open/high/low/close`` are already *split*-adjusted. Without that, Apple's
  4-for-1 split in 2020 would look like a 75% crash and every strategy would "learn"
  to short it.
* They are *not* dividend-adjusted. ``adjclose`` is. Over ten years dividends are a
  large share of total return - roughly a third of the S&P 500's - so ignoring them
  quietly penalises buy-and-hold and any strategy that actually holds things.

So by default this feed scales the whole bar by ``adjclose / close``, giving a
total-return series. Pass ``adjust=False`` to get the raw traded prices instead,
which is what you want if you care about the actual price levels a stop would have
been triggered at.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from ..types import Candle
from .base import Feed, validate_series

BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart"

VALID_RANGES = ("1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max")
VALID_INTERVALS = ("1d", "5d", "1wk", "1mo", "3mo")


class YahooError(RuntimeError):
    pass


class YahooFeed(Feed):
    def __init__(
        self,
        symbol: str = "SPY",
        interval: str = "1d",
        range_: str = "10y",
        adjust: bool = True,
        timeout: float = 30.0,
    ) -> None:
        if interval not in VALID_INTERVALS:
            raise ValueError(f"interval must be one of {', '.join(VALID_INTERVALS)}")
        if range_ not in VALID_RANGES:
            raise ValueError(f"range must be one of {', '.join(VALID_RANGES)}")
        self.symbol = symbol
        self.interval = interval
        self.range = range_
        self.adjust = adjust
        self.timeout = timeout

    def _fetch(self, retries: int = 4) -> dict:
        url = (
            f"{BASE_URL}/{self.symbol}?range={self.range}"
            f"&interval={self.interval}&events=div,split"
        )
        last: Exception | None = None
        for attempt in range(retries):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                error = payload.get("chart", {}).get("error")
                if error:
                    raise YahooError(f"{self.symbol}: {error}")
                return payload
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = exc
                if attempt < retries - 1:
                    time.sleep(2**attempt)
        raise YahooError(f"could not fetch {self.symbol}: {last}") from last

    def load(self) -> list[Candle]:
        return self.parse(self._fetch())

    def parse(self, payload: dict) -> list[Candle]:
        """Turn a Yahoo chart response into candles. Split out so it can be tested."""
        results = payload.get("chart", {}).get("result") or []
        if not results:
            raise YahooError(f"no data returned for {self.symbol}")
        result = results[0]

        timestamps = result.get("timestamp") or []
        quote = (result.get("indicators", {}).get("quote") or [{}])[0]
        adjclose_block = result.get("indicators", {}).get("adjclose") or [{}]
        adjclose = adjclose_block[0].get("adjclose") if adjclose_block else None

        opens = quote.get("open") or []
        highs = quote.get("high") or []
        lows = quote.get("low") or []
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        if not timestamps or not closes:
            raise YahooError(f"{self.symbol}: response contained no price series")

        candles: list[Candle] = []
        skipped = 0
        for i, ts in enumerate(timestamps):
            o, h, low, c = opens[i], highs[i], lows[i], closes[i]
            # Yahoo pads holidays and halted sessions with nulls. A bar with no close
            # is not a zero-return day, it is an absent day, so drop it rather than
            # inventing a price.
            if None in (o, h, low, c):
                skipped += 1
                continue

            factor = 1.0
            if self.adjust and adjclose and i < len(adjclose) and adjclose[i] and c:
                factor = adjclose[i] / c

            volume = volumes[i] if i < len(volumes) and volumes[i] is not None else 0.0
            candles.append(
                Candle(
                    ts=int(ts) * 1000,
                    open=o * factor,
                    high=h * factor,
                    low=low * factor,
                    close=c * factor,
                    volume=float(volume),
                )
            )

        if not candles:
            raise YahooError(f"{self.symbol}: every bar was missing data")
        return validate_series(candles)

    def history(self, limit: int = 10_000) -> list[Candle]:
        return self.load()[-limit:]


def describe_span(candles: list[Candle]) -> str:
    """Human-readable date range of a series, for report headers."""
    if not candles:
        return "no data"
    start = datetime.fromtimestamp(candles[0].ts / 1000, tz=timezone.utc).date()
    end = datetime.fromtimestamp(candles[-1].ts / 1000, tz=timezone.utc).date()
    years = (candles[-1].ts - candles[0].ts) / (365.25 * 86_400_000)
    return f"{start} to {end} ({len(candles):,} bars, {years:.1f} years)"
