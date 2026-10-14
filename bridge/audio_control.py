"""Short, explicit speaker tests delivered through the device snapshot."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


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
        if self.state == "pending" and time.monotonic() >= self.deadline:
            self.state = "timeout"
            self.detail = "设备未在 30 秒内确认，请唤醒 Dotii 并检查连接和固件版本"
            self.updated = int(time.time())

    def test(self) -> dict[str, Any]:
        with self.lock:
            self._expire()
            if self.state == "pending":
                raise RuntimeError("已有扬声器测试正在等待设备确认")
            if self.volume == 0:
                raise ValueError("当前已静音，请先调高音量并保存")
            self.request = {"id": secrets.token_hex(8), "volume": self.volume}
            self.deadline = time.monotonic() + 30
            self.state = "pending"
            self.detail = "等待 Dotii 播放，请保持设备唤醒"
            self.updated = int(time.time())
            return self.public()

    def device_snapshot(self) -> dict[str, Any]:
        with self.lock:
            self._expire()
            pending = None
            if self.state == "pending" and self.request is not None:
                pending = {**self.request, "ttl_ms": max(1, int((self.deadline - time.monotonic()) * 1000))}
            return {"volume": self.volume, "test": pending}

    def report(self, payload: Any) -> dict[str, Any]:
        with self.lock:
            self._expire()
            if not isinstance(payload, dict) or self.request is None or payload.get("id") != self.request["id"]:
                raise ValueError("未知的扬声器测试")
            result = payload.get("result")
            error = payload.get("error", "")
            if not isinstance(result, str) or result not in {"played", "failed"} or not isinstance(error, str) or len(error) > 64:
                raise ValueError("扬声器测试结果无效")
            if self.state not in {"pending", result}:
                raise ValueError("扬声器测试已结束或过期")
            self.state = result
            self.detail = "设备已完成播放，请确认是否听到提示音" if result == "played" else "设备播放失败：" + (error or "未知错误")
            self.updated = int(time.time())
            return self.public()

    def public(self) -> dict[str, Any]:
        with self.lock:
            self._expire()
            return {"volume": self.volume, "state": self.state, "detail": self.detail,
                    "request_id": self.request["id"] if self.request else "", "updated_at_epoch": self.updated}
