"""The local control panel: tier limits, config writing, and the HTTP guard.

The panel starts and stops real OS processes, so the tests that matter are the ones
that pin what it refuses to do: run more than the tier allows, touch a config it did
not write, accept a state-changing request with no guard header, or hand a Pro
feature to a free user.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from tradebot import webapp


class TempProject:
    """A throwaway cwd with configs/ and state/ dirs."""

    def __enter__(self):
        self._prev = os.getcwd()
        self.dir = tempfile.mkdtemp()
        os.chdir(self.dir)
        Path("configs").mkdir()
        Path("state").mkdir()
        return self

    def __exit__(self, *exc):
        os.chdir(self._prev)


# --------------------------------------------------------------------- licensing


class TestLicensing(unittest.TestCase):
    def test_issue_then_verify_round_trips(self):
        for seed in (0, 1, 12345, 2**63 - 1):
            self.assertTrue(webapp.verify_license(webapp.issue_license(seed)))

    def test_garbage_is_rejected(self):
        for bad in ("", "pro", "TB-PRO-", "TB-PRO-XYZ", "TB-PRO-0000000000000000-000000000000"):
            self.assertFalse(webapp.verify_license(bad))

    def test_a_tampered_tag_is_rejected(self):
        key = webapp.issue_license(42)
        head, tag = key.rsplit("-", 1)
        flipped = ("0" if tag[0] != "0" else "1") + tag[1:]
        self.assertFalse(webapp.verify_license(f"{head}-{flipped}"))

    def test_case_and_whitespace_are_tolerated(self):
        key = webapp.issue_license(7)
        self.assertTrue(webapp.verify_license(f"  {key.lower()}  "))

    def test_resolve_tier_reads_the_environment(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIs(webapp.resolve_tier(), webapp.FREE)
        with mock.patch.dict(os.environ, {"TRADEBOT_LICENSE": webapp.issue_license(1)}):
            self.assertIs(webapp.resolve_tier(), webapp.PRO)
        with mock.patch.dict(os.environ, {"TRADEBOT_LICENSE": "nonsense"}):
            self.assertIs(webapp.resolve_tier(), webapp.FREE)


class TestTierShape(unittest.TestCase):
    def test_free_is_the_restricted_one(self):
        self.assertLess(webapp.FREE.max_running, webapp.PRO.max_running)
        self.assertFalse(webapp.FREE.can_arm_live)
        self.assertNotIn("alpaca", webapp.FREE.venues)
        self.assertIsNone(webapp.PRO.strategies)

    def test_allows_strategy(self):
        self.assertTrue(webapp.FREE.allows_strategy("slow_trend"))
        self.assertFalse(webapp.FREE.allows_strategy("micro_scalp"))
        self.assertTrue(webapp.PRO.allows_strategy("micro_scalp"))


# ------------------------------------------------------------------ config writing


class TestCreate(unittest.TestCase):
    def test_it_writes_a_loadable_config_with_a_safe_name(self):
        with TempProject():
            m = webapp.Manager(tier=webapp.FREE)
            name = m.create({"name": "My BTC Test!!", "strategy": "slow_trend",
                             "venue": "paper", "interval": "1h", "starting_cash": 500})
            self.assertEqual(name, "web_my-btc-test")
            self.assertTrue((Path("configs") / "web_my-btc-test.toml").exists())

    def test_a_traversal_name_cannot_escape_the_configs_dir(self):
        with TempProject():
            m = webapp.Manager(tier=webapp.FREE)
            name = m.create({"name": "../../etc/passwd", "strategy": "buy_and_hold", "venue": "paper"})
            self.assertTrue(name.startswith("web_"))
            self.assertNotIn("/", name)
            self.assertNotIn("..", name)
            written = list(Path("configs").glob("*.toml"))
            self.assertEqual([p.parent.name for p in written], ["configs"])

    def test_a_blank_name_is_refused(self):
        with TempProject():
            m = webapp.Manager(tier=webapp.FREE)
            with self.assertRaises(ValueError):
                m.create({"name": "  ", "strategy": "buy_and_hold", "venue": "paper"})

    def test_duplicate_names_are_refused(self):
        with TempProject():
            m = webapp.Manager(tier=webapp.FREE)
            m.create({"name": "dup", "strategy": "buy_and_hold", "venue": "paper"})
            with self.assertRaises(ValueError):
                m.create({"name": "dup", "strategy": "buy_and_hold", "venue": "paper"})

    def test_free_tier_blocks_pro_strategies_and_alpaca(self):
        with TempProject():
            m = webapp.Manager(tier=webapp.FREE)
            with self.assertRaises(ValueError):
                m.create({"name": "a", "strategy": "micro_scalp", "venue": "paper"})
            with self.assertRaises(ValueError):
                m.create({"name": "b", "strategy": "slow_trend", "venue": "alpaca"})

    def test_pro_tier_allows_both(self):
        with TempProject():
            m = webapp.Manager(tier=webapp.PRO)
            m.create({"name": "a", "strategy": "micro_scalp", "venue": "paper"})
            name = m.create({"name": "b", "strategy": "vol_target", "venue": "alpaca",
                             "symbol": "ETH/USD"})
            text = (Path("configs") / f"{name}.toml").read_text()
            self.assertIn("enabled = true", text)

    def test_bad_interval_and_cash_are_refused(self):
        with TempProject():
            m = webapp.Manager(tier=webapp.PRO)
            with self.assertRaises(ValueError):
                m.create({"name": "a", "strategy": "buy_and_hold", "venue": "paper", "interval": "3m"})
            with self.assertRaises(ValueError):
                m.create({"name": "b", "strategy": "buy_and_hold", "venue": "paper", "starting_cash": -5})


class TestDelete(unittest.TestCase):
    def test_delete_refuses_a_config_it_did_not_write(self):
        with TempProject():
            (Path("configs") / "hand_written.toml").write_text("[market]\nsymbol='X'\n")
            m = webapp.Manager(tier=webapp.PRO)
            with self.assertRaises(ValueError):
                m.delete("hand_written")
            self.assertTrue((Path("configs") / "hand_written.toml").exists())

    def test_delete_removes_a_panel_config(self):
        with TempProject():
            m = webapp.Manager(tier=webapp.PRO)
            name = m.create({"name": "gone", "strategy": "buy_and_hold", "venue": "paper"})
            m.delete(name)
            self.assertFalse((Path("configs") / f"{name}.toml").exists())


class TestStartCap(unittest.TestCase):
    def test_free_tier_refuses_a_second_running_session(self):
        with TempProject():
            m = webapp.Manager(tier=webapp.FREE)
            m.create({"name": "one", "strategy": "buy_and_hold", "venue": "paper"})
            m.create({"name": "two", "strategy": "buy_and_hold", "venue": "paper"})
            with mock.patch.object(webapp, "_pid_alive", return_value=True):
                fake = mock.Mock()
                fake.pid = 4242
                fake.poll.return_value = None
                with mock.patch("subprocess.Popen", return_value=fake):
                    m.start("web_one")
                with self.assertRaises(ValueError):
                    m.start("web_two")


# --------------------------------------------------------------------------- http


class ServerFixture(unittest.TestCase):
    def serve(self, tier):
        self.project = TempProject().__enter__()
        self.srv = webapp.serve(port=0, tier=tier)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        time.sleep(0.1)

    def tearDown(self):
        if getattr(self, "srv", None):
            self.srv.shutdown()
            self.srv.server_close()
        if getattr(self, "project", None):
            self.project.__exit__()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path):
        return urllib.request.urlopen(self._url(path)).read().decode()

    def post(self, path, body, guard=True):
        headers = {"Content-Type": "application/json"}
        if guard:
            headers["X-Tradebot"] = "panel"
        req = urllib.request.Request(self._url(path), data=json.dumps(body).encode(),
                                     method="POST", headers=headers)
        return urllib.request.urlopen(req).read().decode()


class TestHttpGuard(ServerFixture):
    def test_a_post_without_the_panel_header_is_refused(self):
        self.serve(webapp.FREE)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post("/api/sessions", {"name": "x", "strategy": "buy_and_hold", "venue": "paper"},
                      guard=False)
        self.assertEqual(cm.exception.code, 403)

    def test_the_page_and_read_endpoints_serve(self):
        self.serve(webapp.FREE)
        self.assertIn("tradebot panel", self.get("/"))
        self.assertIn("buy_and_hold", self.get("/api/strategies"))
        self.assertEqual(json.loads(self.get("/api/overview"))["tier"]["name"], "free")

    def test_export_needs_pro(self):
        self.serve(webapp.FREE)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/api/export/anything")
        self.assertEqual(cm.exception.code, 402)


if __name__ == "__main__":
    unittest.main()
