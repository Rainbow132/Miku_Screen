# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import time

from utils.logger import logger


class ConfigStore:
    """mtime-based JSON reader that keeps the last valid configuration."""

    def __init__(self, path, defaults=None):
        self.path = os.fspath(path)
        self._value = dict(defaults or {})
        self._mtime_ns = None
        self._last_warning_at = 0.0

    def get(self):
        try:
            mtime_ns = os.stat(self.path).st_mtime_ns
            if mtime_ns == self._mtime_ns:
                return self._value
            with open(self.path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if not isinstance(loaded, dict):
                raise ValueError("configuration root must be an object")
            merged = dict(self._value)
            merged.update(loaded)
            self._value = merged
            self._mtime_ns = mtime_ns
        except FileNotFoundError:
            self._warn_limited(f"????????{self.path}")
        except Exception as exc:
            self._warn_limited(f"???????????????????{exc}")
        return self._value

    def _warn_limited(self, message):
        now = time.monotonic()
        if not self._last_warning_at or now - self._last_warning_at >= 30.0:
            logger.warning(message)
            self._last_warning_at = now
