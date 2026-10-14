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
    METRICS = {"sample_rate": 16000, "samples": 80000, "peak": 5000, "rms": 900, "clipped_samples": 0}

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

    def recording_report(self, request: dict, result: str, **extra: object) -> dict:
        return {"id": request["request_id"], "kind": "record_playback", "result": result, **extra}

    def test_recording_is_invisible_to_old_firmware_and_never_restarts_implicitly(self) -> None:
        request = self.audio.test("record_playback")
        snapshot = self.audio.device_snapshot()
        self.assertIsNone(snapshot["test"])
        self.assertEqual(snapshot["record_test"]["id"], request["request_id"])
        restarted = AudioControl(self.path)
        self.assertIsNone(restarted.device_snapshot()["record_test"])
        self.assertIsNone(restarted.public()["metrics"])

    def test_recording_progress_keeps_audio_exclusive_and_consumes_command(self) -> None:
        request = self.audio.test("record_playback")
        for state in ("recording", "playing"):
            self.audio.report(self.recording_report(request, state, metrics=self.METRICS))
            self.assertIsNone(self.audio.device_snapshot()["record_test"])
            for kind in ("tone", "record_playback"):
                with self.assertRaises(RuntimeError):
                    self.audio.test(kind)
        played = self.recording_report(request, "played", metrics=self.METRICS)
        self.assertEqual(self.audio.report(played)["metrics"], self.METRICS)
        self.assertEqual(self.audio.report(played)["state"], "played")
        self.assertIsNone(self.audio.test()["metrics"])

    def test_recording_requires_matching_kind_and_complete_bounded_capture_metrics(self) -> None:
        request = self.audio.test("record_playback")
        with self.assertRaises(ValueError):
            self.audio.report({"id": request["request_id"], "result": "played"})
        for metrics in (None, {}, {**self.METRICS, "samples": 79999},
                        {**self.METRICS, "rms": True}, {**self.METRICS, "peak": 32769},
                        {**self.METRICS, "rms": 6000}, {**self.METRICS, "sample_rate": 22050}):
            with self.subTest(metrics=metrics), self.assertRaises(ValueError):
                self.audio.report(self.recording_report(request, "played", metrics=metrics))
        self.assertEqual(self.audio.public()["state"], "pending")

    def test_progress_cannot_move_backwards_or_reopen_a_finished_test(self) -> None:
        request = self.audio.test("record_playback")
        self.audio.report(self.recording_report(request, "playing", metrics=self.METRICS))
        with self.assertRaises(ValueError):
            self.audio.report(self.recording_report(request, "recording"))
        self.audio.report(self.recording_report(request, "failed", error="270"))
        with self.assertRaises(ValueError):
            self.audio.report(self.recording_report(request, "played", metrics=self.METRICS))

    def test_in_progress_recording_still_times_out_and_rejects_late_result(self) -> None:
        with mock.patch("audio_control.time.monotonic", return_value=100):
            request = self.audio.test("record_playback")
            self.audio.report(self.recording_report(request, "recording"))
        with mock.patch("audio_control.time.monotonic", return_value=131):
            self.assertEqual(self.audio.public()["state"], "timeout")
            with self.assertRaises(ValueError):
                self.audio.report(self.recording_report(request, "played", metrics=self.METRICS))

    def test_quiet_capture_reports_weak_signal_without_claiming_speech_recognition(self) -> None:
        request = self.audio.test("record_playback")
        result = self.audio.report(self.recording_report(request, "played", metrics={**self.METRICS, "rms": 30}))
        self.assertIn("输入信号较弱", result["detail"])


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

    def test_record_endpoint_limits_access_and_requires_authenticated_capture_result(self) -> None:
        path = "/api/v1/admin/audio/record-test"
        with mock.patch.object(BridgeHandler, "_local_admin", return_value=False):
            self.assertEqual(self.post(path, {})[0], 403)
        self.assertEqual(self.post(path, {}, content_type="text/plain")[0], 400)
        self.server.audio.configure({"volume": 0})
        self.assertEqual(self.post(path, {})[0], 400)
        self.server.audio.configure({"volume": 30})
        status, body = self.post(path, {})
        self.assertEqual(status, 202)
        self.assertEqual(self.post(path, {})[0], 409)
        report = {"id": body["audio"]["request_id"], "kind": "record_playback", "result": "recording"}
        self.assertEqual(self.post("/api/v1/audio/result", report)[0], 401)
        self.assertEqual(self.post("/api/v1/audio/result", report, token=self.server.token)[0], 200)
        report.update(result="played", metrics=AudioControlTests.METRICS)
        self.assertEqual(self.post("/api/v1/audio/result", report, token=self.server.token)[0], 200)


if __name__ == "__main__":
    unittest.main()
