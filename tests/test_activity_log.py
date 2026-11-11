"""Activity log + built-in web dashboard tests."""
import json
import os
import socket
import tempfile
import unittest
import urllib.request

_TMP = tempfile.mkdtemp(prefix="terabox-test-")
os.environ["DATA_DIR"] = _TMP
os.environ["BOT_DB_PATH"] = os.path.join(_TMP, "test.sqlite3")
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "test-hash")
os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ["WEB_DASHBOARD_WEBHOOK_URL"] = ""  # forwarder off by default in tests

import requests  # noqa: E402

import config  # noqa: E402
from services import activity_log, web_dashboard  # noqa: E402


def _reset_state():
    with activity_log._lock:
        activity_log._events.clear()
        activity_log._pending.clear()
        for key in activity_log._counters:
            activity_log._counters[key] = 0
        activity_log._next_id = 1
        activity_log._last_event_at = None
        activity_log._net_times.clear()
        activity_log._net_suppressed = 0


class ActivityLogTest(unittest.TestCase):
    def setUp(self):
        _reset_state()

    def test_record_and_query_roundtrip(self):
        first = activity_log.record_event("info", "Bot started", source="bot")
        second = activity_log.record_upload(
            "video.mp4", 734003200, "https://1024terabox.com/s/x",
            118.4, source="batch", target="SSC CGL",
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)

        events = activity_log.query_events()
        self.assertEqual(len(events), 2)
        # Newest first
        self.assertEqual(events[0]["id"], second)
        self.assertEqual(events[0]["type"], "upload")
        self.assertEqual(events[0]["file_name"], "video.mp4")
        self.assertEqual(events[0]["file_size"], 734003200)
        self.assertEqual(events[0]["link"], "https://1024terabox.com/s/x")
        self.assertEqual(events[0]["source"], "batch")
        self.assertEqual(events[0]["target"], "SSC CGL")
        self.assertEqual(events[0]["duration_sec"], 118.4)
        self.assertIn("video.mp4", events[0]["message"])
        # ISO timestamp format
        self.assertRegex(events[0]["created_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_type_filter_and_after_watermark(self):
        activity_log.record_upload("a.mp4", 10, "https://x/1", 1.0)
        activity_log.record_network_error("timeout", "ReadTimeout")
        activity_log.record_upload("b.mp4", 20, "https://x/2", 2.0)

        only_uploads = activity_log.query_events(event_type="upload")
        self.assertEqual(len(only_uploads), 2)
        self.assertTrue(all(e["type"] == "upload" for e in only_uploads))

        after_first = activity_log.query_events(after=1)
        ids = [e["id"] for e in after_first]
        self.assertNotIn(1, ids)
        self.assertIn(2, ids)
        self.assertIn(3, ids)

    def test_stats_counters(self):
        activity_log.record_upload("a", 1, "l", 1.0)
        activity_log.record_upload("b", 1, "l", 1.0)
        activity_log.record_upload_failed("c", 1, "cookie expired")
        activity_log.record_network_error("boom", "reset")
        activity_log.record_event("info", "hello")
        s = activity_log.stats()
        self.assertEqual(s["uploads"], 2)
        self.assertEqual(s["upload_failed"], 1)
        self.assertEqual(s["network_errors"], 1)
        self.assertEqual(s["infos"], 1)
        self.assertEqual(s["total"], 5)
        self.assertIsNotNone(s["last_event_at"])

    def test_api_payload_shape(self):
        activity_log.record_upload("a.mp4", 500, "https://x/1", 2.5)
        payload = activity_log.api_payload()
        self.assertTrue(payload["ok"])
        self.assertIn("events", payload)
        self.assertIn("stats", payload)
        self.assertIn("server_time", payload)
        self.assertEqual(payload["stats"]["uploads"], 1)
        self.assertEqual(payload["events"][0]["type"], "upload")

    def test_invalid_inputs_never_raise(self):
        self.assertIsNone(activity_log.record_event("bogus_type", "x"))
        self.assertIsNone(activity_log.record_event("upload", "   "))
        self.assertIsNone(activity_log.record_event(None, None))
        # No exception = pass

    def test_network_error_rate_limit(self):
        recorded = 0
        for i in range(20):
            eid = activity_log.record_network_error(f"err {i}", "timeout")
            if eid is not None:
                recorded += 1
        # Burst window caps at NETWORK_BURST_MAX
        self.assertEqual(recorded, activity_log.NETWORK_BURST_MAX)

    def test_is_network_exception(self):
        self.assertTrue(activity_log.is_network_exception(requests.Timeout("read timed out")))
        self.assertTrue(activity_log.is_network_exception(requests.ConnectionError("refused")))
        self.assertTrue(activity_log.is_network_exception(ConnectionResetError()))
        self.assertTrue(activity_log.is_network_exception(TimeoutError()))

        class FloodWait(Exception):
            pass

        self.assertTrue(activity_log.is_network_exception(FloodWait("Wait 30 seconds")))
        self.assertTrue(activity_log.is_network_exception(Exception("SSL handshake failed")))
        # Non-network failures
        self.assertFalse(activity_log.is_network_exception(ValueError("bad value")))
        self.assertFalse(activity_log.is_network_exception(Exception("cookie expired errno=-6")))

    def test_record_failure_classifies(self):
        activity_log.record_failure(requests.Timeout("read timed out"), name="f.mp4", size=10)
        activity_log.record_failure(ValueError("bad value"), name="g.mp4", size=10)
        s = activity_log.stats()
        self.assertEqual(s["network_errors"], 1)
        self.assertEqual(s["upload_failed"], 1)

    def test_webhook_forward_batch(self):
        calls = []

        class FakeResponse:
            status_code = 200

        def fake_post(url, data=None, headers=None, timeout=None):
            calls.append({"url": url, "data": json.loads(data), "headers": headers})
            return FakeResponse()

        original_post = activity_log.requests.post
        original_url = config.WEB_DASHBOARD_WEBHOOK_URL
        original_token = config.WEB_DASHBOARD_WEBHOOK_TOKEN
        activity_log.requests.post = fake_post
        config.WEB_DASHBOARD_WEBHOOK_URL = "https://dashboard.example/api/events"
        config.WEB_DASHBOARD_WEBHOOK_TOKEN = "sekrit"
        try:
            activity_log.record_upload("w.mp4", 42, "https://x/w", 3.0, source="batch")
            activity_log.record_network_error("net fail", "reset")
            self.assertEqual(len(activity_log._pending), 2)
            activity_log._forward_batch()
        finally:
            activity_log.requests.post = original_post
            config.WEB_DASHBOARD_WEBHOOK_URL = original_url
            config.WEB_DASHBOARD_WEBHOOK_TOKEN = original_token
            with activity_log._lock:
                activity_log._pending.clear()

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["url"], "https://dashboard.example/api/events")
        self.assertEqual(calls[0]["headers"]["x-dashboard-token"], "sekrit")
        sent = calls[0]["data"]["events"]
        self.assertEqual(len(sent), 2)
        self.assertEqual({e["type"] for e in sent}, {"upload", "network_error"})
        # Sent events drain from the queue
        self.assertEqual(len(activity_log._pending), 0)


class WebDashboardTest(unittest.TestCase):
    def setUp(self):
        _reset_state()

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_page_file_exists_and_polls_api(self):
        page = web_dashboard._PAGE_PATH.read_text(encoding="utf-8")
        self.assertIn("TeraFast", page)
        self.assertIn("/api/logs", page)

    def test_server_serves_page_and_api(self):
        port = self._free_port()
        old_port = config.WEB_DASHBOARD_PORT
        old_enabled = config.WEB_DASHBOARD_ENABLED
        config.WEB_DASHBOARD_PORT = port
        config.WEB_DASHBOARD_ENABLED = True
        try:
            self.assertTrue(web_dashboard.start())
            activity_log.record_upload("v.mp4", 1024, "https://x/v", 5.0, source="direct")

            base = f"http://127.0.0.1:{port}"

            with urllib.request.urlopen(f"{base}/api/health") as res:
                health = json.loads(res.read())
            self.assertTrue(health["ok"])
            self.assertEqual(health["stats"]["uploads"], 1)

            with urllib.request.urlopen(f"{base}/api/logs?limit=10") as res:
                logs = json.loads(res.read())
            self.assertTrue(logs["ok"])
            self.assertEqual(logs["stats"]["uploads"], 1)
            self.assertEqual(logs["events"][0]["file_name"], "v.mp4")

            with urllib.request.urlopen(
                f"{base}/api/logs?type=network_error&limit=10"
            ) as res:
                empty = json.loads(res.read())
            self.assertEqual(empty["events"], [])

            with urllib.request.urlopen(base + "/") as res:
                html = res.read().decode("utf-8")
            self.assertIn("TeraFast", html)
        finally:
            web_dashboard.stop()
            config.WEB_DASHBOARD_PORT = old_port
            config.WEB_DASHBOARD_ENABLED = old_enabled

    def test_server_disabled_by_config(self):
        old_enabled = config.WEB_DASHBOARD_ENABLED
        config.WEB_DASHBOARD_ENABLED = False
        try:
            self.assertFalse(web_dashboard.start())
        finally:
            config.WEB_DASHBOARD_ENABLED = old_enabled


if __name__ == "__main__":
    unittest.main()
