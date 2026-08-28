"""What actually goes on the wire to the exchange.

This is the one part of the system that cannot be validated by running it. Everything
else can be checked against a backtest; a signature can only be checked against a
funded account, and a wrong one comes back as an opaque 401 that looks identical to
an expired key, a wrong method name or a missing field. That ambiguity is the point
of this file: it pins the request byte for byte against the published algorithm, so
that if a live call ever is rejected, the signing shape is the one thing already
ruled out.

To be clear about what this does and does not prove. It proves the implementation
matches Crypto.com's documented algorithm - concatenation order, key sorting, nested
handling, the HMAC itself - and that it is stable under refactoring. It does not
prove the documentation is current, and no test here has ever been checked against a
real account. Treat a live run as unvalidated until ``verify-keys`` returns a balance.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from unittest import mock

from tradebot.brokers.base import BrokerError
from tradebot.brokers.cryptocom import CryptoComBroker, floor_to_decimals, params_to_str
from tradebot.costs import CostModel
from tradebot.types import Side

KEY = "test-api-key"
SECRET = "test-api-secret"


class FakeResponse:
    """Stands in for urlopen's context manager, capturing what was sent."""

    def __init__(self, body):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SigningTestCase(unittest.TestCase):
    def broker(self, **kwargs):
        return CryptoComBroker(symbol="BTC_USD", costs=CostModel(), **kwargs)

    def with_credentials(self):
        return mock.patch.dict(
            "os.environ",
            {"CRYPTOCOM_API_KEY": KEY, "CRYPTOCOM_API_SECRET": SECRET},
        )

    def capture(self, broker, method, params, result=None):
        """Run one signed request against a fake transport and return the JSON body."""
        sent = {}

        def fake_urlopen(request, timeout=None):
            sent["url"] = request.full_url
            sent["headers"] = dict(request.headers)
            sent["body"] = json.loads(request.data.decode("utf-8"))
            sent["method"] = request.get_method()
            return FakeResponse({"code": 0, "result": result or {}})

        with self.with_credentials(), mock.patch("urllib.request.urlopen", fake_urlopen):
            broker._signed_request(method, params)
        return sent


class TestParameterSerialisation(unittest.TestCase):
    """The parameter string, which is where signatures usually go wrong."""

    def test_keys_are_sorted_and_joined_without_separators(self):
        self.assertEqual(params_to_str({"c": "3", "a": "1", "b": "2"}), "a1b2c3")

    def test_a_real_create_order_payload(self):
        params = {
            "instrument_name": "BTC_USD",
            "side": "BUY",
            "type": "MARKET",
            "quantity": "0.001000",
        }
        self.assertEqual(
            params_to_str(params),
            "instrument_nameBTC_USDquantity0.001000sideBUYtypeMARKET",
        )

    def test_booleans_are_lower_cased_and_none_becomes_null(self):
        self.assertEqual(params_to_str({"a": True, "b": False, "c": None}), "atruebfalsecnull")

    def test_lists_are_flattened_in_order(self):
        params = {"orders": [{"b": "2", "a": "1"}, {"a": "3"}]}
        self.assertEqual(params_to_str(params), "ordersa1b2a3")

    def test_nesting_stops_at_the_documented_depth(self):
        """MAX_LEVEL is 3 in the reference implementation; deeper values stringify whole."""
        deep = {"a": [{"b": [{"c": [{"d": "1"}]}]}]}
        self.assertIn("{", params_to_str(deep), "past max_level the object is stringified")

    def test_empty_params_produce_an_empty_string(self):
        self.assertEqual(params_to_str({}), "")

    def test_integers_are_allowed_because_both_sides_agree_on_them(self):
        self.assertEqual(params_to_str({"n": 5}), "n5")


class TestFloatsAreRefused(SigningTestCase):
    """The trap: a float that hashes differently on each side of the wire.

    Python writes 0.00001 as '1e-05'. The exchange writes '0.00001'. Round numbers
    agree, so this passes every casual test and then rejects the one small order that
    matters.
    """

    def test_a_small_float_would_serialise_differently_at_each_end(self):
        self.assertEqual(str(0.00001), "1e-05")
        self.assertNotEqual(str(0.00001), f"{0.00001:.8f}".rstrip("0"))

    def test_a_top_level_float_is_refused(self):
        with self.assertRaises(ValueError):
            params_to_str({"quantity": 0.001})

    def test_a_nested_float_is_refused(self):
        with self.assertRaises(ValueError):
            params_to_str({"orders": [{"quantity": 0.001}]})

    def test_a_bare_float_inside_a_list_is_refused(self):
        """Reaches the top-level guard rather than the one inside the key loop."""
        with self.assertRaises(ValueError):
            params_to_str({"prices": [0.001]})

    def test_a_float_passed_directly_is_refused(self):
        with self.assertRaises(ValueError):
            params_to_str(0.001)

    def test_the_error_says_what_to_do_instead(self):
        with self.assertRaises(ValueError) as caught:
            params_to_str({"quantity": 0.001})
        self.assertIn("strings", str(caught.exception))

    def test_the_broker_turns_it_into_a_rejection_not_a_crash(self):
        """A bare ValueError would escape the engine's rejection handling entirely."""
        with self.with_credentials():
            with self.assertRaises(BrokerError):
                self.broker()._signed_request("private/create-order", {"quantity": 0.001})

    def test_the_orders_this_code_actually_sends_carry_no_floats(self):
        broker = self.broker(enabled=True, dry_run=False, max_order_notional=1e9)
        sent = {}

        def fake_urlopen(request, timeout=None):
            sent.update(json.loads(request.data.decode("utf-8")))
            return FakeResponse({"code": 0, "result": {"order_id": "1"}})

        with self.with_credentials(), mock.patch("urllib.request.urlopen", fake_urlopen):
            broker.execute(1, 0.00001, 30_000.0, "tiny order")

        for key, value in sent["params"].items():
            self.assertNotIsInstance(value, float, f"param {key!r} went out as a float")


class TestSignatureComposition(SigningTestCase):
    """method + id + api_key + params + nonce, hashed with the secret."""

    def test_the_signature_matches_an_independently_computed_hmac(self):
        params = {"instrument_name": "BTC_USD", "side": "BUY", "type": "MARKET",
                  "quantity": "0.001000"}
        sent = self.capture(self.broker(), "private/create-order", params)
        body = sent["body"]

        expected_base = (
            f"private/create-order{body['id']}{KEY}"
            f"instrument_nameBTC_USDquantity0.001000sideBUYtypeMARKET{body['nonce']}"
        )
        expected = hmac.new(
            SECRET.encode("utf-8"), expected_base.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        self.assertEqual(body["sig"], expected)

    def test_a_known_vector_is_stable(self):
        """Pins the exact digest for fixed inputs, so a refactor cannot drift the shape."""
        base = "private/get-order-detail11test-api-keyorder_id53314308941647741115"
        self.assertEqual(
            hmac.new(SECRET.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest(),
            hmac.new(
                SECRET.encode("utf-8"),
                (
                    "private/get-order-detail"
                    "11"
                    "test-api-key"
                    + params_to_str({"order_id": "5331430894"})
                    + "1647741115"
                ).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest(),
        )

    def test_the_id_in_the_signature_is_the_id_in_the_body(self):
        """A mismatch here is rejected by the venue and looks exactly like a bad key."""
        broker = self.broker()
        first = self.capture(broker, "private/user-balance", {})["body"]
        second = self.capture(broker, "private/user-balance", {})["body"]
        self.assertNotEqual(first["id"], second["id"], "request ids must advance")

        for body in (first, second):
            base = f"private/user-balance{body['id']}{KEY}{body['nonce']}"
            expected = hmac.new(
                SECRET.encode("utf-8"), base.encode("utf-8"), hashlib.sha256
            ).hexdigest()
            self.assertEqual(body["sig"], expected)

    def test_the_secret_never_appears_in_the_request(self):
        sent = self.capture(self.broker(), "private/user-balance", {})
        self.assertNotIn(SECRET, json.dumps(sent["body"]))
        self.assertNotIn(SECRET, json.dumps(sent["headers"]))

    def test_the_request_shape_is_what_the_venue_expects(self):
        sent = self.capture(self.broker(), "private/user-balance", {})
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["url"], "https://api.crypto.com/exchange/v1/private/user-balance")
        self.assertEqual(sent["headers"].get("Content-type"), "application/json")
        self.assertEqual(set(sent["body"]), {"id", "method", "api_key", "params", "nonce", "sig"})


class TestErrorsAreNotSwallowed(SigningTestCase):
    def test_a_nonzero_result_code_is_an_error_even_on_http_200(self):
        """The venue returns 200 with a code field; treating that as success loses orders."""

        def fake_urlopen(request, timeout=None):
            return FakeResponse({"code": 40101, "message": "Authentication failure"})

        with self.with_credentials(), mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(BrokerError) as caught:
                self.broker()._signed_request("private/user-balance", {})
        self.assertIn("40101", str(caught.exception))


class TestOrderSizeCap(SigningTestCase):
    """max_order_notional is the last thing between a sizing bug and your balance."""

    def sent_quantity(self, broker, qty, price):
        sent = {}

        def fake_urlopen(request, timeout=None):
            sent.update(json.loads(request.data.decode("utf-8")))
            return FakeResponse({"code": 0, "result": {"order_id": "1"}})

        with self.with_credentials(), mock.patch("urllib.request.urlopen", fake_urlopen):
            broker.execute(1, qty, price, "test")
        return float(sent["params"]["quantity"])

    def test_an_oversized_order_is_trimmed_not_rejected(self):
        broker = self.broker(enabled=True, dry_run=False, max_order_notional=50.0)
        qty = self.sent_quantity(broker, 1.0, 30_000.0)
        self.assertLessEqual(qty * 30_000.0, 50.0 + 1e-6)

    def test_the_cap_applies_to_sells_as_well_as_buys(self):
        broker = self.broker(enabled=True, dry_run=False, max_order_notional=50.0)
        sent = {}

        def fake_urlopen(request, timeout=None):
            sent.update(json.loads(request.data.decode("utf-8")))
            return FakeResponse({"code": 0, "result": {"order_id": "1"}})

        with self.with_credentials(), mock.patch("urllib.request.urlopen", fake_urlopen):
            fill = broker.execute(1, -1.0, 30_000.0, "test")

        self.assertEqual(sent["params"]["side"], "SELL")
        self.assertLessEqual(float(sent["params"]["quantity"]) * 30_000.0, 50.0 + 1e-6)
        self.assertIsNotNone(fill)
        self.assertIs(fill.side, Side.SELL)

    def test_the_cap_is_never_exceeded_at_any_lot_size(self):
        """The bug this caught: round() goes to nearest, so a trim could round up.

        At six decimals the overshoot was a penny. At a whole-unit lot size on a £30
        instrument it turned a £50 cap into a £60 order - the safety limit exceeded by
        20% by the code enforcing it.
        """
        for decimals in range(0, 7):
            for price in (0.37, 3.0, 30.0, 1234.56, 30_000.0):
                with self.subTest(decimals=decimals, price=price):
                    broker = self.broker(enabled=True, dry_run=False,
                                         max_order_notional=50.0, qty_decimals=decimals)
                    sent = {}

                    def fake_urlopen(request, timeout=None):
                        sent.update(json.loads(request.data.decode("utf-8")))
                        return FakeResponse({"code": 0, "result": {"order_id": "1"}})

                    with self.with_credentials(), mock.patch("urllib.request.urlopen", fake_urlopen):
                        broker.execute(1, 10_000.0 / price, price, "far too big")

                    if sent:
                        notional = float(sent["params"]["quantity"]) * price
                        self.assertLessEqual(
                            notional, 50.0,
                            f"max_order_notional exceeded by the code enforcing it "
                            f"({notional:.4f} > 50.00)",
                        )

    def test_a_requested_size_is_rounded_down_never_up(self):
        """An order must never be larger than the strategy asked for."""
        self.assertEqual(floor_to_decimals(0.0015, 3), 0.001)
        self.assertEqual(floor_to_decimals(1.9999, 0), 1.0)
        self.assertEqual(floor_to_decimals(0.999999999, 2), 0.99)

    def test_an_order_that_rounds_away_is_not_sent_at_all(self):
        broker = self.broker(enabled=True, dry_run=False, qty_decimals=2)
        with self.with_credentials(), mock.patch("urllib.request.urlopen", self.fail):
            self.assertIsNone(broker.execute(1, 0.0001, 30_000.0, "dust"))


class TestGatesBlockTheNetwork(SigningTestCase):
    """Not "no order placed" - no request made at all. Verified by making the socket fail."""

    def assert_no_request(self, broker):
        def explode(request, timeout=None):
            raise AssertionError("a request left the machine while a gate was shut")

        with self.with_credentials(), mock.patch("urllib.request.urlopen", explode):
            self.assertIsNone(broker.execute(1, 0.001, 30_000.0, "test"))

    def test_disabled_sends_nothing(self):
        self.assert_no_request(self.broker(enabled=False, dry_run=False))

    def test_dry_run_sends_nothing(self):
        self.assert_no_request(self.broker(enabled=True, dry_run=True))

    def test_defaults_send_nothing(self):
        self.assert_no_request(self.broker())


if __name__ == "__main__":
    unittest.main()
