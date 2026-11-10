# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone

import psutil

from utils.logger import logger


class SystemAgent:
    """Read disk information and cache the LHM bridge's loopback snapshot."""

    SNAPSHOT_URL = "http://127.0.0.1:16035/snapshot"
    POLL_INTERVAL_SECONDS = 1.0
    REQUEST_TIMEOUT_SECONDS = 0.25
    MAX_SNAPSHOT_AGE_SECONDS = 5.0
    CPU_GAME_THRESHOLD = 30.0
    GPU_GAME_THRESHOLD = 75.0

    def __init__(self, snapshot_url=None):
        self.snapshot_url = snapshot_url or self.SNAPSHOT_URL
        self.cached_mode = "daily"
        self._snapshot = None
        self._snapshot_received_at = 0.0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._last_error_log_at = 0.0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="lhm-snapshot-reader",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None

    def _poll_loop(self):
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                request = urllib.request.Request(
                    self.snapshot_url,
                    headers={"Accept": "application/json", "Cache-Control": "no-cache"},
                )
                with urllib.request.urlopen(
                    request, timeout=self.REQUEST_TIMEOUT_SECONDS
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                snapshot = self._validate_snapshot(payload)
                with self._lock:
                    self._snapshot = snapshot
                    self._snapshot_received_at = time.monotonic()
                self._last_error_log_at = 0.0
            except Exception as exc:
                now = time.monotonic()
                if not self._last_error_log_at or now - self._last_error_log_at >= 60.0:
                    logger.warning(
                        "[System] LHM ???????????????? daily: %s", exc
                    )
                    self._last_error_log_at = now

            remaining = self.POLL_INTERVAL_SECONDS - (time.monotonic() - started)
            self._stop_event.wait(max(0.05, remaining))

    @classmethod
    def _validate_snapshot(cls, payload):
        if not isinstance(payload, dict):
            raise ValueError("snapshot is not an object")

        timestamp_text = payload.get("timestampUtc")
        if not isinstance(timestamp_text, str):
            raise ValueError("snapshot timestamp is missing")
        iso_text = timestamp_text.replace("Z", "+00:00")
        # .NET emits seven fractional digits; Python 3.9 accepts at most six.
        if "." in iso_text:
            prefix, fractional_and_zone = iso_text.split(".", 1)
            zone_at = next(
                (index for index, char in enumerate(fractional_and_zone) if char in "+-"),
                len(fractional_and_zone),
            )
            fraction = fractional_and_zone[:zone_at]
            zone = fractional_and_zone[zone_at:]
            iso_text = f"{prefix}.{fraction[:6]}{zone}"
        timestamp = datetime.fromisoformat(iso_text)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
        if age > cls.MAX_SNAPSHOT_AGE_SECONDS or age < -30.0:
            raise ValueError(f"stale snapshot ({age:.1f}s)")

        normalized = dict(payload)
        for key in ("cpuLoad", "gpuLoad"):
            value = payload.get(key)
            if not isinstance(value, (int, float)):
                raise ValueError(f"{key} is missing")
            normalized[key] = float(value)
        return normalized

    def get_hardware_snapshot(self):
        with self._lock:
            snapshot = dict(self._snapshot) if self._snapshot else None
            received_at = self._snapshot_received_at
        if snapshot is None or time.monotonic() - received_at > self.MAX_SNAPSHOT_AGE_SECONDS:
            return None
        return snapshot

    def get_disk_info(self):
        """Return free space for the system disk and its used ratio."""
        try:
            path = "C:\\" if os.name == "nt" else "/"
            usage = psutil.disk_usage(path)
            free_gb = usage.free / (1024 ** 3)
            total_gb = usage.total / (1024 ** 3)
            percent = (usage.total - usage.free) / usage.total
            return f"C: {free_gb:.0f}G / {total_gb:.0f}G Free", percent
        except Exception:
            return "C: ---G / ---G Free", 0.0

    def get_battery_info(self):
        return {
            "kb_battery": "85%",
            "ms_battery": "45%",
            "gp_battery": "60%",
            "hp_battery": "70%",
        }

    def get_hardware_load_mode(self):
        """Classify load using the same LHM sample already sent to SignalRGB."""
        snapshot = self.get_hardware_snapshot()
        if snapshot is None:
            self.cached_mode = "daily"
            return self.cached_mode

        cpu_usage = snapshot["cpuLoad"]
        gpu_usage = snapshot["gpuLoad"]
        self.cached_mode = (
            "game"
            if cpu_usage > self.CPU_GAME_THRESHOLD
            and gpu_usage > self.GPU_GAME_THRESHOLD
            else "daily"
        )
        return self.cached_mode
