"""Explicit tone and device-local recording tests delivered in snapshots."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ACTIVE_STATES = {"pending", "recording", "playing"}


def validate_recording_metrics(payload: Any) -> dict[str, int]:
    if not isinstance(payload, dict):
        raise ValueError("缺少录音采样结果")
    limits = {"sample_rate": (16000, 16000), "samples": (80000, 80000),
              "peak": (0, 32768), "rms": (0, 32768), "clipped_samples": (0, 80000)}
    result = {}
    for name, (low, high) in limits.items():
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError("录音采样结果无效")
        result[name] = value
    if result["rms"] > result["peak"]:
        raise ValueError("录音采样结果无效")
    return result


def validate_volume(payload: Any) -> int:
    volume = payload.get("volume") if isinstance(payload, dict) else None
    if isinstance(volume, bool) or not isinstance(volume, int) or not 0 <= volume <= 50:
        raise ValueError("音量必须是 0–50 的整数，0 表示静音")
    return volume


class AudioControl:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.volume = 15
        if path.is_file():
            self.volume = validate_volume(json.loads(path.read_text(encoding="utf-8")))
        self.request: dict[str, Any] | None = None
        self.deadline = 0.0
        self.state = "idle"
        self.detail = "点击测试，Dotii 将播放半秒提示音"
        self.updated = 0
        self.metrics: dict[str, int] | None = None

    def configure(self, payload: Any) -> dict[str, Any]:
        volume = validate_volume(payload)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix="audio-", suffix=".json", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump({"volume": volume}, stream)
                    stream.write("\n")
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self.volume = volume
            return self.public()

    def _expire(self) -> None:
        if self.state in ACTIVE_STATES and time.monotonic() >= self.deadline:
            self.state = "timeout"
            self.detail = "设备未在 30 秒内确认，请唤醒 Dotii 并检查连接和固件版本"
            self.updated = int(time.time())

    def test(self, kind: str = "tone") -> dict[str, Any]:
        with self.lock:
            if kind not in {"tone", "record_playback"}:
                raise ValueError("未知的音频测试")
            self._expire()
            if self.state in ACTIVE_STATES:
                raise RuntimeError("已有音频测试正在执行，请等待完成")
            if self.volume == 0:
                raise ValueError("当前已静音，请先调高音量并保存")
            self.request = {"id": secrets.token_hex(8), "volume": self.volume, "kind": kind}
            self.metrics = None
            self.deadline = time.monotonic() + 30
            self.state = "pending"
            self.detail = ("等待 Dotii，听到提示音后说话 5 秒，随后自动回放" if kind == "record_playback"
                           else "等待 Dotii 播放，请保持设备唤醒")
            self.updated = int(time.time())
            return self.public()

    def device_snapshot(self) -> dict[str, Any]:
        with self.lock:
            self._expire()
            pending = None
            if self.state == "pending" and self.request is not None:
                pending = {**self.request, "ttl_ms": max(1, int((self.deadline - time.monotonic()) * 1000))}
            recording = pending is not None and pending["kind"] == "record_playback"
            # A 1.1.2 device must never mistake a recording request for a tone.
            return {"volume": self.volume, "test": None if recording else pending,
                    "record_test": pending if recording else None}

    def report(self, payload: Any) -> dict[str, Any]:
        with self.lock:
            self._expire()
            if not isinstance(payload, dict) or self.request is None or payload.get("id") != self.request["id"]:
                raise ValueError("未知的音频测试")
            result = payload.get("result")
            error = payload.get("error", "")
            recording = self.request["kind"] == "record_playback"
            allowed = {"recording", "playing", "played", "failed"} if recording else {"played", "failed"}
            if payload.get("kind", "tone") != self.request["kind"]:
                raise ValueError("音频测试类型不匹配")
            if not isinstance(result, str) or result not in allowed or not isinstance(error, str) or len(error) > 64:
                raise ValueError("音频测试结果无效")
            transitions = {"pending": allowed, "recording": allowed,
                           "playing": {"playing", "played", "failed"}, "played": {"played"}, "failed": {"failed"}}
            if result not in transitions.get(self.state, set()):
                raise ValueError("音频测试已结束或过期")
            metrics = None
            if recording and result in {"playing", "played"}:
                metrics = validate_recording_metrics(payload.get("metrics"))
            if metrics is not None:
                self.metrics = metrics
            self.state = result
            details = {"recording": "正在录音 5 秒，请对着 Dotii 说话",
                       "playing": "录音结束，正在回放，请听一下 Dotii 扬声器",
                       "played": "录音回放已完成，请确认是否听到刚才说的话" if recording else "设备已完成播放，请确认是否听到提示音",
                       "failed": "音频测试失败：" + (error or "未知错误")}
            self.detail = details[result]
            if result == "played" and self.metrics is not None:
                if self.metrics["rms"] < 100:
                    self.detail += "；输入信号较弱，请靠近麦克风重试"
                elif self.metrics["clipped_samples"] > 800:
                    self.detail += "；输入有削波，请稍微远离麦克风"
            self.updated = int(time.time())
            return self.public()

    def public(self) -> dict[str, Any]:
        with self.lock:
            self._expire()
            return {"volume": self.volume, "state": self.state, "detail": self.detail,
                    "kind": self.request["kind"] if self.request else "tone", "metrics": self.metrics,
                    "request_id": self.request["id"] if self.request else "", "updated_at_epoch": self.updated}
