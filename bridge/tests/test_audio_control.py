from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio_control import AudioControl, validate_volume
from codex_bridge import BridgeHandler


class AudioControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "audio.json"
        self.audio = AudioControl(self.path)

    def test_settings_persist_but_pending_tests_do_not_survive_restart(self) -> None:
        self.audio.configure({"volume": 20})
        self.audio.test()
        restored = AudioControl(self.path)
        self.assertEqual(restored.public()["volume"], 20)
        self.assertIsNone(restored.device_snapshot()["test"])

    def test_bounds_and_muted_tests(self) -> None:
        for value in (-1, 51, True, 1.5, "15", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_volume({"volume": value})
        self.audio.configure({"volume": 0})
        with self.assertRaises(ValueError):
            self.audio.test()

    def test_one_pending_request_and_authenticated_result_consumes_it(self) -> None:
        request = self.audio.test()
        with self.assertRaises(RuntimeError):
            self.audio.test()
        device = self.audio.device_snapshot()["test"]
        self.assertEqual(device["id"], request["request_id"])
        self.assertGreater(device["ttl_ms"], 0)
        with self.assertRaises(ValueError):
            self.audio.report({"id": "other", "result": "played"})
        report = {"id": device["id"], "result": "played"}
        self.assertEqual(self.audio.report(report)["state"], "played")
        self.assertEqual(self.audio.report(report)["state"], "played")
        self.assertIsNone(self.audio.device_snapshot()["test"])
        with self.assertRaises(ValueError):
            self.audio.report({"id": device["id"], "result": "failed"})

    def test_expiry_removes_command_and_rejects_late_acknowledgment(self) -> None:
        with mock.patch("audio_control.time.monotonic", return_value=100):
            request = self.audio.test()
        with mock.patch("audio_control.time.monotonic", return_value=131):
            self.assertIsNone(self.audio.device_snapshot()["test"])
            self.assertEqual(self.audio.public()["state"], "timeout")
            with self.assertRaises(ValueError):
                self.audio.report({"id": request["request_id"], "result": "played"})

    def test_device_failure_is_not_reported_as_success(self) -> None:
        request = self.audio.test()
        result = self.audio.report({"id": request["request_id"], "result": "failed", "error": "263"})
        self.assertEqual(result["state"], "failed")
        self.assertIn("263", result["detail"])
        self.assertIsNone(self.audio.device_snapshot()["test"])

    def test_malformed_result_cannot_crash_or_consume_pending_test(self) -> None:
        request = self.audio.test()
        for result in ([], {}, None, "unknown"):
            with self.subTest(result=result), self.assertRaises(ValueError):
                self.audio.report({"id": request["request_id"], "result": result})
        self.assertEqual(self.audio.public()["state"], "pending")


class AudioApiTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), BridgeHandler)
        self.server.audio = AudioControl(Path(temporary.name) / "audio.json")
        self.server.token = "test-only-token-not-a-real-device-secret"
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, path: str, payload: dict, *, token: str = "", content_type: str = "application/json") -> tuple[int, dict]:
        connection = http.client.HTTPConnection(*self.server.server_address)
        try:
            connection.request("POST", path, json.dumps(payload),
                               {"Content-Type": content_type, "X-Bridge-Token": token})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_real_http_request_to_device_result_requires_token(self) -> None:
        status, body = self.post("/api/v1/admin/audio/test", {})
        self.assertEqual(status, 202)
        report = {"id": body["audio"]["request_id"], "result": "played"}
        self.assertEqual(self.post("/api/v1/audio/result", report)[0], 401)
        self.assertEqual(self.server.audio.public()["state"], "pending")
        self.assertEqual(self.post("/api/v1/audio/result", report, token=self.server.token)[0], 200)
        self.assertEqual(self.server.audio.public()["state"], "played")

    def test_admin_is_loopback_only_and_requires_json(self) -> None:
        with mock.patch.object(BridgeHandler, "_local_admin", return_value=False):
            self.assertEqual(self.post("/api/v1/admin/audio/test", {})[0], 403)
            self.assertEqual(self.post("/api/v1/admin/audio/config", {"volume": 20})[0], 403)
        self.assertEqual(self.post("/api/v1/admin/audio/test", {}, content_type="text/plain")[0], 400)
        self.assertEqual(self.post("/api/v1/admin/audio/config", {"volume": 100})[0], 400)
        self.assertEqual(self.post("/api/v1/admin/audio/config", {"volume": 20})[0], 200)
        self.assertEqual(self.post("/api/v1/admin/audio/test", {})[0], 202)
        self.assertEqual(self.post("/api/v1/admin/audio/test", {})[0], 409)


if __name__ == "__main__":
    unittest.main()
