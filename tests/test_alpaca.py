"""What tradebot sends to Alpaca, and what it makes of the replies.

Like test_broker_signing, none of this has been checked against a real Alpaca
account. It pins the request shape and the response parsing against Alpaca's
documented REST API and keeps them stable under refactoring. Treat a live or paper
run as unvalidated until `verify-keys` returns an account.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from tradebot.brokers.alpaca import AlpacaBroker
from tradebot.brokers.base import BrokerError
from tradebot.config import AlpacaConfig
from tradebot.costs import CostModel
from tradebot.feeds.alpaca import AlpacaFeed, _parse_ts
from tradebot.feeds.cryptocom import FeedError
from tradebot.types import Side

KEY = "test-key-id"
SECRET = "test-secret-key"


def creds():
    return mock.patch.dict("os.environ", {"APCA_API_KEY_ID": KEY, "APCA_API_SECRET_KEY": SECRET})


class FakeResponse:
    def __init__(self, body):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Transport:
    """Routes urlopen calls by (method, path) to canned JSON, recording each request."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, request, timeout=None):
        method = request.get_method()
        url = request.full_url
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        self.calls.append({"method": method, "url": url, "headers": dict(request.headers), "body": body})
        for (m, needle), response in self.routes.items():
            if m == method and needle in url:
                payload = response(self) if callable(response) else response
                return FakeResponse(payload)
        raise AssertionError(f"no route for {method} {url}")


# --------------------------------------------------------------------------- feed


class TestTimestampParsing(unittest.TestCase):
    def test_rfc3339_z_suffix_becomes_epoch_ms(self):
        self.assertEqual(_parse_ts("1970-01-01T00:00:00Z"), 0)
        self.assertEqual(_parse_ts("1970-01-01T00:00:01Z"), 1000)

    def test_offset_is_respected(self):
        self.assertEqual(_parse_ts("1970-01-01T01:00:00+01:00"), 0)


class TestFeedConstruction(unittest.TestCase):
    def test_unknown_interval_is_rejected(self):
        with self.assertRaises(ValueError):
            AlpacaFeed(interval="3m")

    def test_unknown_asset_class_is_rejected(self):
        with self.assertRaises(ValueError):
            AlpacaFeed(asset_class="forex")

    def test_interval_ms_matches_the_interval(self):
        self.assertEqual(AlpacaFeed(interval="1h").interval_ms, 3_600_000)


class TestFeedRequests(unittest.TestCase):
    def _history(self, feed, bars):
        transport = Transport({("GET", "/bars"): {"bars": {feed.symbol: bars}, "next_page_token": None}})
        with creds(), mock.patch("urllib.request.urlopen", transport):
            candles = feed.history(10)
        return candles, transport

    def test_crypto_hits_the_crypto_endpoint_with_the_symbol(self):
        feed = AlpacaFeed(symbol="BTC/USD", interval="1h", asset_class="crypto")
        bars = [{"t": "2024-01-01T00:00:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 5}]
        _, transport = self._history(feed, bars)
        url = transport.calls[0]["url"]
        self.assertIn("v1beta3/crypto/us/bars", url)
        self.assertIn("symbols=BTC%2FUSD", url)
        self.assertIn("timeframe=1Hour", url)
        self.assertNotIn("feed=", url)

    def test_equities_hit_the_stocks_endpoint_with_the_data_feed(self):
        feed = AlpacaFeed(symbol="AAPL", interval="5m", asset_class="us_equity", data_feed="iex")
        bars = [{"t": "2024-01-01T00:00:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 5}]
        _, transport = self._history(feed, bars)
        url = transport.calls[0]["url"]
        self.assertIn("v2/stocks/bars", url)
        self.assertIn("timeframe=5Min", url)
        self.assertIn("feed=iex", url)

    def test_credentials_go_out_as_headers_when_present(self):
        feed = AlpacaFeed(symbol="AAPL", asset_class="us_equity")
        bars = [{"t": "2024-01-01T00:00:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 5}]
        _, transport = self._history(feed, bars)
        headers = transport.calls[0]["headers"]
        # urllib title-cases header keys
        self.assertEqual(headers.get("Apca-api-key-id"), KEY)
        self.assertEqual(headers.get("Apca-api-secret-key"), SECRET)

    def test_history_returns_oldest_first_and_trims_to_limit(self):
        feed = AlpacaFeed(symbol="BTC/USD", interval="1h")
        bars = [
            {"t": f"2024-01-0{i}T00:00:00Z", "o": i, "h": i + 1, "l": i, "c": i + 1, "v": 1}
            for i in range(1, 8)
        ]
        transport = Transport({("GET", "/bars"): {"bars": {"BTC/USD": bars}, "next_page_token": None}})
        with creds(), mock.patch("urllib.request.urlopen", transport):
            candles = feed.history(3)
        self.assertEqual(len(candles), 3)
        self.assertLess(candles[0].ts, candles[-1].ts)
        self.assertEqual(candles[-1].close, 8)

    def test_history_follows_the_page_token(self):
        feed = AlpacaFeed(symbol="BTC/USD", interval="1h")
        page1 = {"bars": {"BTC/USD": [{"t": "2024-01-01T00:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}]},
                 "next_page_token": "abc"}
        page2 = {"bars": {"BTC/USD": [{"t": "2024-01-01T01:00:00Z", "o": 2, "h": 2, "l": 2, "c": 2, "v": 1}]},
                 "next_page_token": None}
        seen = []

        def router(_transport):
            seen.append(1)
            return page1 if len(seen) == 1 else page2

        transport = Transport({("GET", "/bars"): router})
        with creds(), mock.patch("urllib.request.urlopen", transport):
            candles = feed.history(10)
        self.assertEqual(len(transport.calls), 2)
        self.assertIn("page_token=abc", transport.calls[1]["url"])
        self.assertEqual([c.close for c in candles], [1, 2])

    def test_an_empty_series_raises_feed_error(self):
        feed = AlpacaFeed(symbol="BTC/USD", interval="1h")
        transport = Transport({("GET", "/bars"): {"bars": {}, "next_page_token": None}})
        with creds(), mock.patch("urllib.request.urlopen", transport):
            with self.assertRaises(FeedError):
                feed.history(10)

    def test_latest_closed_discards_the_forming_bar(self):
        feed = AlpacaFeed(symbol="BTC/USD", interval="1h")
        # One bar closed an hour ago, one opened just now and is still forming.
        import time as _t
        now = int(_t.time())
        old = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(now - 7200))
        forming = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(now - 60))
        bars = [
            {"t": old, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},
            {"t": forming, "o": 2, "h": 2, "l": 2, "c": 2, "v": 1},
        ]
        transport = Transport({("GET", "/bars"): {"bars": {"BTC/USD": bars}, "next_page_token": None}})
        with creds(), mock.patch("urllib.request.urlopen", transport):
            candle = feed.latest_closed()
        self.assertEqual(candle.close, 1, "the still-forming bar should not be returned")


# ------------------------------------------------------------------------- broker


class TestBrokerIdentity(unittest.TestCase):
    def test_paper_is_not_live(self):
        b = AlpacaBroker(symbol="BTC/USD", costs=CostModel(), paper=True)
        self.assertFalse(b.is_live)
        self.assertIn("paper-api", b.base_url)

    def test_real_money_is_live(self):
        b = AlpacaBroker(symbol="BTC/USD", costs=CostModel(), paper=False)
        self.assertTrue(b.is_live)
        self.assertEqual(b.base_url, "https://api.alpaca.markets")

    def test_bad_asset_class_is_rejected(self):
        with self.assertRaises(ValueError):
            AlpacaBroker(symbol="BTC/USD", costs=CostModel(), asset_class="forex")

    def test_missing_credentials_raise(self):
        b = AlpacaBroker(symbol="BTC/USD", costs=CostModel())
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(BrokerError):
                b.verify()


class TestOrderSubmission(unittest.TestCase):
    def _fill_routes(self, symbol="BTC/USD", filled_qty="0.01", avg="30000"):
        order = {"id": "o-1", "status": "accepted"}
        filled = {"id": "o-1", "status": "filled", "filled_qty": filled_qty, "filled_avg_price": avg}
        return {
            ("POST", "/v2/orders"): order,
            ("GET", "/v2/orders/o-1"): filled,
        }

    def test_a_crypto_market_order_payload(self):
        b = AlpacaBroker(symbol="BTC/USD", costs=CostModel(), asset_class="crypto",
                         paper=True, max_order_notional=1e9, qty_decimals=6)
        transport = Transport(self._fill_routes())
        with creds(), mock.patch("urllib.request.urlopen", transport):
            fill = b.execute(1, 0.01, 30_000.0, "go")
        posted = next(c for c in transport.calls if c["method"] == "POST")["body"]
        self.assertEqual(posted["symbol"], "BTC/USD")
        self.assertEqual(posted["side"], "buy")
        self.assertEqual(posted["type"], "market")
        self.assertEqual(posted["time_in_force"], "gtc")
        self.assertEqual(posted["qty"], "0.010000")
        self.assertIsNotNone(fill)
        self.assertEqual(fill.side, Side.BUY)
        self.assertAlmostEqual(fill.price, 30_000.0)
        self.assertAlmostEqual(fill.qty, 0.01)

    def test_an_equity_order_uses_day_time_in_force(self):
        b = AlpacaBroker(symbol="AAPL", costs=CostModel(), asset_class="us_equity",
                         paper=True, max_order_notional=1e9, qty_decimals=0)
        transport = Transport(self._fill_routes(filled_qty="3", avg="200"))
        with creds(), mock.patch("urllib.request.urlopen", transport):
            b.execute(1, 3, 200.0, "go")
        posted = next(c for c in transport.calls if c["method"] == "POST")["body"]
        self.assertEqual(posted["time_in_force"], "day")
        self.assertEqual(posted["qty"], "3")

    def test_a_sell_is_signed_correctly(self):
        b = AlpacaBroker(symbol="BTC/USD", costs=CostModel(), paper=True, max_order_notional=1e9)
        transport = Transport(self._fill_routes(filled_qty="0.01", avg="30000"))
        with creds(), mock.patch("urllib.request.urlopen", transport):
            fill = b.execute(1, -0.01, 30_000.0, "exit")
        posted = next(c for c in transport.calls if c["method"] == "POST")["body"]
        self.assertEqual(posted["side"], "sell")
        self.assertEqual(fill.side, Side.SELL)

    def test_paper_ignores_the_dry_run_and_enabled_gates(self):
        # paper=True, but enabled defaults False and dry_run defaults True.
        b = AlpacaBroker(symbol="BTC/USD", costs=CostModel(), paper=True, max_order_notional=1e9)
        transport = Transport(self._fill_routes())
        with creds(), mock.patch("urllib.request.urlopen", transport):
            fill = b.execute(1, 0.01, 30_000.0, "go")
        self.assertIsNotNone(fill, "a paper order must go through regardless of the real-money gates")

    def test_real_money_honours_dry_run(self):
        b = AlpacaBroker(symbol="BTC/USD", costs=CostModel(), paper=False,
                         enabled=True, dry_run=True, max_order_notional=1e9)
        transport = Transport(self._fill_routes())
        with creds(), mock.patch("urllib.request.urlopen", transport):
            fill = b.execute(1, 0.01, 30_000.0, "go")
        self.assertIsNone(fill)
        self.assertEqual(transport.calls, [], "dry_run must not send anything")

    def test_real_money_honours_the_enabled_gate(self):
        b = AlpacaBroker(symbol="BTC/USD", costs=CostModel(), paper=False,
                         enabled=False, dry_run=False, max_order_notional=1e9)
        transport = Transport(self._fill_routes())
        with creds(), mock.patch("urllib.request.urlopen", transport):
            self.assertIsNone(b.execute(1, 0.01, 30_000.0, "go"))
        self.assertEqual(transport.calls, [])

    def test_real_money_sends_when_every_gate_is_open(self):
        b = AlpacaBroker(symbol="BTC/USD", costs=CostModel(), paper=False,
                         enabled=True, dry_run=False, max_order_notional=1e9)
        transport = Transport(self._fill_routes())
        with creds(), mock.patch("urllib.request.urlopen", transport):
            fill = b.execute(1, 0.01, 30_000.0, "go")
        self.assertIsNotNone(fill)

    def test_max_order_notional_trims_the_quantity(self):
        b = AlpacaBroker(symbol="BTC/USD", costs=CostModel(), paper=True,
                         max_order_notional=50.0, qty_decimals=6)
        transport = Transport(self._fill_routes(filled_qty="0.001666", avg="30000"))
        with creds(), mock.patch("urllib.request.urlopen", transport):
            b.execute(1, 1.0, 30_000.0, "too big")
        posted = next(c for c in transport.calls if c["method"] == "POST")["body"]
        # 50 / 30000 = 0.001666..., floored to 6dp
        self.assertEqual(posted["qty"], "0.001666")

    def test_an_order_that_never_fills_raises(self):
        b = AlpacaBroker(symbol="BTC/USD", costs=CostModel(), paper=True,
                         max_order_notional=1e9, fill_wait_seconds=0.0)
        routes = {
            ("POST", "/v2/orders"): {"id": "o-1", "status": "accepted"},
            ("GET", "/v2/orders/o-1"): {"id": "o-1", "status": "accepted", "filled_avg_price": None},
        }
        with creds(), mock.patch("urllib.request.urlopen", Transport(routes)):
            with self.assertRaises(BrokerError):
                b.execute(1, 0.01, 30_000.0, "go")

    def test_a_dust_order_is_skipped_without_a_request(self):
        b = AlpacaBroker(symbol="BTC/USD", costs=CostModel(), paper=True)
        transport = Transport({})
        with creds(), mock.patch("urllib.request.urlopen", transport):
            self.assertIsNone(b.execute(1, 1e-15, 30_000.0, "dust"))
        self.assertEqual(transport.calls, [])


class TestPositionAndAccount(unittest.TestCase):
    def test_sync_position_matches_the_slashless_symbol_and_signs_shorts(self):
        b = AlpacaBroker(symbol="BTC/USD", costs=CostModel(), paper=True)
        routes = {("GET", "/v2/positions"): [
            {"symbol": "ETHUSD", "qty": "2", "side": "long"},
            {"symbol": "BTCUSD", "qty": "0.5", "side": "long"},
        ]}
        with creds(), mock.patch("urllib.request.urlopen", Transport(routes)):
            self.assertAlmostEqual(b.sync_position(), 0.5)

        routes = {("GET", "/v2/positions"): [{"symbol": "BTC/USD", "qty": "0.5", "side": "short"}]}
        with creds(), mock.patch("urllib.request.urlopen", Transport(routes)):
            self.assertAlmostEqual(b.sync_position(), -0.5)

    def test_sync_position_is_zero_when_flat(self):
        b = AlpacaBroker(symbol="BTC/USD", costs=CostModel(), paper=True)
        with creds(), mock.patch("urllib.request.urlopen", Transport({("GET", "/v2/positions"): []})):
            self.assertEqual(b.sync_position(), 0.0)

    def test_verify_reports_the_account_kind(self):
        b = AlpacaBroker(symbol="BTC/USD", costs=CostModel(), paper=True)
        routes = {("GET", "/v2/account"): {"status": "ACTIVE", "cash": "100000", "currency": "USD"}}
        with creds(), mock.patch("urllib.request.urlopen", Transport(routes)):
            summary = b.verify()
        self.assertIn("paper", summary)
        self.assertIn("ACTIVE", summary)


class TestAlpacaConfig(unittest.TestCase):
    def test_defaults_are_off_and_crypto_paper(self):
        c = AlpacaConfig()
        self.assertFalse(c.enabled)
        self.assertEqual(c.asset_class, "crypto")
        self.assertTrue(c.paper)

    def test_bad_asset_class_is_rejected(self):
        with self.assertRaises(ValueError):
            AlpacaConfig(asset_class="forex")

    def test_bad_data_feed_is_rejected(self):
        with self.assertRaises(ValueError):
            AlpacaConfig(data_feed="realtime")


if __name__ == "__main__":
    unittest.main()
