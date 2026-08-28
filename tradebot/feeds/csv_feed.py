"""Read bars from a CSV file.

Accepts the usual column names case-insensitively. Timestamps may be epoch seconds,
epoch milliseconds, or an ISO 8601 string - all three turn up in exported data and
guessing wrong shifts your whole series, so the parser is explicit about each.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from ..types import Candle
from .base import Feed, validate_series

_ALIASES = {
    "ts": ("ts", "time", "timestamp", "date", "datetime", "open_time", "t"),
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c", "price"),
    "volume": ("volume", "v", "vol", "base_volume"),
}


def _pick(row: dict[str, str], field: str) -> str | None:
    for alias in _ALIASES[field]:
        if alias in row and row[alias] not in (None, ""):
            return row[alias]
    return None


def parse_timestamp(raw: str) -> int:
    """Return epoch milliseconds from seconds, milliseconds or an ISO 8601 string."""
    text = raw.strip()
    try:
        number = float(text)
    except ValueError:
        cleaned = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(cleaned)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)

    # A plain integer is ambiguous. Anything past ~2001 in milliseconds is far larger
    # than any plausible epoch-seconds value, so the magnitude settles it.
    if number > 1e11:
        return int(number)
    return int(number * 1000)


class CsvFeed(Feed):
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"no such CSV file: {self.path}")

    def load(self) -> list[Candle]:
        candles: list[Candle] = []
        with self.path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ValueError(f"{self.path} has no header row")
            for line_no, raw_row in enumerate(reader, start=2):
                row = {(k or "").strip().lower(): v for k, v in raw_row.items()}
                try:
                    close = _pick(row, "close")
                    if close is None:
                        raise ValueError("missing a close/price column")
                    ts_raw = _pick(row, "ts")
                    if ts_raw is None:
                        raise ValueError("missing a timestamp column")
                    open_ = _pick(row, "open") or close
                    high = _pick(row, "high") or close
                    low = _pick(row, "low") or close
                    volume = _pick(row, "volume") or "0"
                    candles.append(
                        Candle(
                            ts=parse_timestamp(ts_raw),
                            open=float(open_),
                            high=float(high),
                            low=float(low),
                            close=float(close),
                            volume=float(volume),
                        )
                    )
                except (ValueError, KeyError) as exc:
                    raise ValueError(f"{self.path}:{line_no}: {exc}") from exc
        return validate_series(candles)

    def history(self, limit: int) -> list[Candle]:
        return self.load()[-limit:]


def write_csv(path: str | Path, candles: list[Candle]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["ts", "open", "high", "low", "close", "volume"])
        for candle in candles:
            writer.writerow(
                [candle.ts, candle.open, candle.high, candle.low, candle.close, candle.volume]
            )
    return target
