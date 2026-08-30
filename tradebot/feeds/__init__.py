"""Market data feeds."""

from .alpaca import AlpacaFeed
from .base import Feed, validate_series
from .cryptocom import CryptoComFeed, FeedError
from .csv_feed import CsvFeed, write_csv
from .synthetic import SyntheticFeed
from .yahoo import YahooError, YahooFeed, describe_span

__all__ = [
    "Feed",
    "validate_series",
    "AlpacaFeed",
    "CryptoComFeed",
    "FeedError",
    "CsvFeed",
    "write_csv",
    "SyntheticFeed",
    "YahooFeed",
    "YahooError",
    "describe_span",
]
