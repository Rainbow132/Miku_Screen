# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Sequence

from utils.logger import logger

try:
    import hid
except ImportError:
    hid = None

# ==================== Hardware protocol constants ====================
# These values are device protocol and must not be changed casually.
CONTROLLER_VID = 0x3537
CONTROLLER_PID = 0x100B
CONTROLLER_USAGE_PAGE = 65520
CONTROLLER_WAKEUP_CMD = [0x0F, 0x04, 0x00, 0x41] + [0x00] * 60
CONTROLLER_REPORT_ID = 0x12
CONTROLLER_BATTERY_OFFSET = 36
CONTROLLER_CHARGE_OFFSET = 35

MOUSE_VID = 0x1915
MOUSE_PID = 0x0723
MOUSE_USAGE_PAGE = 65290
MOUSE_QUERY_CMD_HIGH_HZ = [0x00, 65, 0, 129, 1] + [0] * 58 + [222]

KEYBOARD_VID = 0x3554
KEYBOARD_PID = 0xFA09
KEYBOARD_USAGE_PAGE = 65282
KEYBOARD_QUERY_CMD = [0x13, 0x05, 0x01] + [0] * 16 + [0x19]
KEYBOARD_REPORT_ID = 0x13
KEYBOARD_STATUS_ID = 0x0A
KEYBOARD_BATTERY_OFFSET = 6
KEYBOARD_STATUS_OFFSET = 7

# HECATE GX03 Ultra 2.4 GHz receiver. The device exposes several vendor
# collections with the same usage page; only MI_04&Col03 carries the TWS
# battery protocol used by the official HECATE application.
GX03_VID = 0x35BB
GX03_PID = 0xA217
GX03_USAGE_PAGE = 0xFF02
GX03_PATH_MARKER = "MI_04&COL03"
GX03_QUERY_CMD = [0xD0, 0x35, 0xBB, 0x01, 0x00]
GX03_REPORT_ID = 0xD0
GX03_RESPONSE_OPCODE = 0x00
GX03_LEFT_BATTERY_OFFSET = 6
GX03_RIGHT_BATTERY_OFFSET = 7
# Observed to match the charging-case percentage shown by the HECATE app.
GX03_CASE_BATTERY_OFFSET = 8

BTH_BATTERY_KEY = "{104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2"
BLUETOOTH_HEADSET_HINTS = (
    "HEADSET", "HEADPHONE", "EARPHONE", "AUDIO", "??", "EDIFIER", "W820"
)

HID_QUERY_TIMEOUT = 0.6
HID_RETRY_INTERVAL = 0.15
HID_READ_INTERVAL = 0.01
HID_DRAIN_LIMIT = 32
ERROR_LOG_INTERVAL = 600.0

# Headset presence is used for routing FxSound, so it must be checked much
# more often than the low-frequency battery refresh used by other peripherals.
HEADSET_POLL_INTERVAL = 10.0

# A wireless timeout is not proof that the device is offline. Keep the last
# known-good value for two transient failures, retrying much sooner than the
# normal battery polling interval. The third transient failure becomes OFF.
TRANSIENT_FAILURE_LIMIT = 3
TRANSIENT_RETRY_INTERVALS = (15.0, 30.0)
FAILED_RETRY_INTERVAL = 300.0
OFFLINE_RETRY_INTERVAL = 300.0

# A sleeping wireless peripheral normally does not remove its USB receiver, so
# USB arrival/removal notifications cannot reliably tell us when it wakes. Use
# Windows' last-input timestamp as a lightweight wake hint instead: after a
# meaningful idle gap, schedule one keyboard/mouse refresh after a short settle
# delay. This observes system input only; it never opens or holds a HID handle.
INPUT_RESUME_IDLE_SECONDS = 60.0
INPUT_RESUME_COOLDOWN_SECONDS = 120.0
INPUT_RESUME_SETTLE_SECONDS = 0.4
INPUT_RESUME_KEYS = ("kb_battery", "ms_battery")

# Never send vendor HID battery commands while Windows is idle. Even though
# each query opens the interface only briefly and always closes it, some
# wireless firmware may treat a host command as activity and postpone its own
# sleep timer. Input-resume detection below brings one refresh forward once the
# user becomes active again.
IDLE_HID_SUPPRESS_SECONDS = 60.0
IDLE_HID_RECHECK_SECONDS = 300.0
IDLE_HID_GUARDED_KEYS = frozenset(INPUT_RESUME_KEYS)


class HidEnumerationError(RuntimeError):
    """The operating system/HIDAPI failed while enumerating interfaces."""


def is_case_battery_low(value: object, threshold: int = 25) -> bool:
    """Return whether a valid case percentage is below the low-battery threshold."""
    if not isinstance(value, str) or not value.endswith("%"):
        return False
    try:
        percent = int(value[:-1])
    except ValueError:
        return False
    return 0 <= percent <= 100 and percent < threshold


@dataclass(frozen=True)
class BatterySample:
    """A protocol-level battery sample, independent from UI formatting."""

    percent: Optional[int] = None
    charging: bool = False
    online: bool = False
    case_percent: Optional[int] = None

    def display_value(self):
        """Convert to the legacy strings consumed by the screen and lighting."""
        if not self.online:
            return "OFF"
        if self.percent is None:
            return "+CHG" if self.charging else "OFF"
        prefix = "+" if self.charging else ""
        return "%s%d%%" % (prefix, self.percent)


@dataclass(frozen=True)
class QueryOutcome:
    """Result of one transport query, including failure classification."""

    sample: Optional[BatterySample] = None
    error_code: Optional[str] = None
    error: Optional[str] = None
    interface_present: Optional[bool] = None

    @property
    def succeeded(self):
        return self.sample is not None and self.error_code is None


@dataclass
class BatteryState:
    """Long-lived device state with freshness and failure information."""

    sample: BatterySample = field(default_factory=BatterySample)
    last_attempt_at: Optional[float] = None
    last_success_at: Optional[float] = None
    consecutive_failures: int = 0
    stale: bool = False
    error_code: Optional[str] = None
    error: Optional[str] = None
    interface_present: Optional[bool] = None


@dataclass(frozen=True)
class HidDeviceSpec:
    """Data-driven wiring for a known vendor HID battery protocol."""

    key: str
    name: str
    vid: int
    pid: int
    usage_page: int
    command: Sequence[int]
    parser_name: str
    normal_interval: float
    charging_interval: Optional[float] = None


HID_DEVICE_SPECS = {
    "kb_battery": HidDeviceSpec(
        "kb_battery", "Keyboard", KEYBOARD_VID, KEYBOARD_PID,
        KEYBOARD_USAGE_PAGE, KEYBOARD_QUERY_CMD, "_parse_keyboard_report",
        600.0, 120.0,
    ),
    "ms_battery": HidDeviceSpec(
        "ms_battery", "Mouse", MOUSE_VID, MOUSE_PID,
        MOUSE_USAGE_PAGE, MOUSE_QUERY_CMD_HIGH_HZ, "_parse_mouse_report",
        300.0,
    ),
    "gp_battery": HidDeviceSpec(
        "gp_battery", "Controller", CONTROLLER_VID, CONTROLLER_PID,
        CONTROLLER_USAGE_PAGE, CONTROLLER_WAKEUP_CMD,
        "_parse_controller_report", 600.0, 120.0,
    ),
}
HID_POLL_ORDER = ("kb_battery", "ms_battery", "gp_battery")
BATTERY_KEYS = ("kb_battery", "ms_battery", "gp_battery", "hp_battery")


def find_hid_interface(vid, pid, target_usage_page):
    """Return the vendor HID path matching VID/PID and usage page.

    A missing interface returns None. An enumeration failure raises a typed
    exception so callers do not incorrectly diagnose an API/driver failure as
    an unplugged device.
    """
    if not hid:
        return None

    try:
        devices = hid.enumerate(vid, pid)
    except Exception as exc:
        raise HidEnumerationError(
            "hid.enumerate failed: %s: %s" % (type(exc).__name__, exc)
        ) from exc

    for dev in devices:
        path = dev.get("path")
        if not path:
            continue

        if isinstance(path, bytes):
            path_text = path.decode("utf-8", errors="ignore").upper()
        else:
            path_text = str(path).upper()

        usage_page = dev.get("usage_page", 0)
        if usage_page != target_usage_page:
            continue

        # Avoid Windows keyboard/mouse collection aliases. The actual vendor
        # control collections used by these devices have neither marker.
        if "KBD" in path_text or "MOU" in path_text:
            continue

        return path

    return None


def find_gx03_interface():
    """Return GX03's battery/control HID collection, never a sibling one."""
    if not hid:
        return None

    try:
        devices = hid.enumerate(GX03_VID, GX03_PID)
    except Exception as exc:
        raise HidEnumerationError(
            "hid.enumerate failed: %s: %s" % (type(exc).__name__, exc)
        ) from exc

    for dev in devices:
        path = dev.get("path")
        if not path or dev.get("usage_page", 0) != GX03_USAGE_PAGE:
            continue

        if isinstance(path, bytes):
            path_text = path.decode("utf-8", errors="ignore").upper()
        else:
            path_text = str(path).upper()

        if GX03_PATH_MARKER in path_text:
            return path

    return None


class WindowsBluetoothBatteryProvider:
    """Windows registry provider, deliberately separate from HID transport."""

    @staticmethod
    def _registry_battery_value(value):
        if isinstance(value, int):
            return value if 0 <= value <= 100 else None
        if isinstance(value, (bytes, bytearray)) and value:
            candidate = int(value[0])
            return candidate if 0 <= candidate <= 100 else None
        return None

    def query(self):
        if os.name != "nt":
            return QueryOutcome(
                error_code="unsupported_platform",
                error="Bluetooth registry provider requires Windows",
                interface_present=False,
            )

        try:
            import winreg
        except ImportError:
            return QueryOutcome(
                error_code="provider_unavailable",
                error="winreg is unavailable",
                interface_present=False,
            )

        candidates = []
        roots = (
            r"SYSTEM\CurrentControlSet\Enum\BTHLEDevice",
            r"SYSTEM\CurrentControlSet\Enum\BTHENUM",
        )

        def walk(root_handle, subkey, inherited_name="", depth=0):
            if depth > 8:
                return
            try:
                with winreg.OpenKey(root_handle, subkey) as key:
                    current_name = inherited_name
                    for value_name in ("FriendlyName", "DeviceDesc"):
                        try:
                            value, _ = winreg.QueryValueEx(key, value_name)
                            if value:
                                current_name = str(value)
                                break
                        except OSError:
                            pass

                    try:
                        value, _ = winreg.QueryValueEx(key, BTH_BATTERY_KEY)
                        battery = self._registry_battery_value(value)
                        if battery is not None:
                            candidates.append((current_name, battery, subkey))
                    except OSError:
                        pass

                    index = 0
                    while True:
                        try:
                            child = winreg.EnumKey(key, index)
                        except OSError:
                            break
                        walk(
                            root_handle,
                            subkey + "\\" + child,
                            current_name,
                            depth + 1,
                        )
                        index += 1
            except OSError:
                return

        for root_path in roots:
            walk(winreg.HKEY_LOCAL_MACHINE, root_path)

        preferred = []
        for name, battery, path in candidates:
            upper_name = name.upper()
            if any(hint.upper() in upper_name for hint in BLUETOOTH_HEADSET_HINTS):
                preferred.append((name, battery, path))

        if not preferred:
            return QueryOutcome(
                error_code="battery_unavailable",
                error="battery property not available",
                interface_present=False,
            )

        preferred.sort(key=lambda item: (item[0].upper(), item[2].upper()))
        return QueryOutcome(
            sample=BatterySample(percent=preferred[0][1], online=True),
            interface_present=True,
        )


class Gx03HidBatteryProvider:
    """Short-lived GX03 HID query compatible with the official HECATE app."""

    def __init__(self, stop_event=None):
        self._stop_event = stop_event

    @staticmethod
    def _parse_report(data):
        # Keep hp_battery conservative: use the lower valid percentage from
        # either bud. Byte 8 is separately exposed as the case percentage; this
        # offset was cross-checked against the official HECATE app display, but
        # is not backed by a published protocol document.
        if len(data) < 8:
            return None
        if (
            data[0] != GX03_REPORT_ID
            or data[1] != 0x35
            or data[2] != 0xBB
            or data[3] < 0x09
            or data[4] != GX03_RESPONSE_OPCODE
            or data[5] == 0
        ):
            return None

        values = [
            int(value)
            for value in (
                data[GX03_LEFT_BATTERY_OFFSET],
                data[GX03_RIGHT_BATTERY_OFFSET],
            )
            if 0 <= int(value) <= 100
        ]
        if not values:
            return None

        case_percent = None
        if len(data) > GX03_CASE_BATTERY_OFFSET:
            candidate = int(data[GX03_CASE_BATTERY_OFFSET])
            if 0 <= candidate <= 100:
                case_percent = candidate

        return BatterySample(
            percent=min(values),
            online=True,
            case_percent=case_percent,
        )

    def _cancelled(self):
        return self._stop_event is not None and self._stop_event.is_set()

    def _wait(self, seconds):
        if self._stop_event is None:
            time.sleep(seconds)
            return False
        return self._stop_event.wait(seconds)

    def query(self):
        if not hid:
            return QueryOutcome(
                error_code="hid_unavailable",
                error="hidapi is not installed",
                interface_present=None,
            )

        if self._cancelled():
            return QueryOutcome(error_code="cancelled")

        try:
            path = find_gx03_interface()
        except HidEnumerationError as exc:
            return QueryOutcome(
                error_code="enumeration_failed",
                error=str(exc),
                interface_present=None,
            )

        if not path:
            return QueryOutcome(
                error_code="not_found",
                error="GX03 Col03 HID interface not found",
                interface_present=False,
            )

        device = None
        try:
            device = hid.device()
            device.open_path(path)
            device.set_nonblocking(True)

            for _ in range(HID_DRAIN_LIMIT):
                if not device.read(64):
                    break

            written = device.write(GX03_QUERY_CMD)
            if not written:
                return QueryOutcome(
                    error_code="write_failed",
                    error="GX03 HID write returned 0 bytes",
                    interface_present=True,
                )

            deadline = time.monotonic() + HID_QUERY_TIMEOUT
            while time.monotonic() < deadline:
                if self._cancelled():
                    return QueryOutcome(error_code="cancelled")

                data = device.read(64)
                if data:
                    sample = self._parse_report(data)
                    if sample is not None:
                        return QueryOutcome(
                            sample=sample,
                            interface_present=True,
                        )

                if self._wait(HID_READ_INTERVAL):
                    return QueryOutcome(error_code="cancelled")

            return QueryOutcome(
                error_code="timeout",
                error="GX03 response timeout after %.0f ms"
                % (HID_QUERY_TIMEOUT * 1000),
                interface_present=True,
            )
        except Exception as exc:
            return QueryOutcome(
                error_code="io_error",
                error="%s: %s" % (type(exc).__name__, exc),
                interface_present=True,
            )
        finally:
            if device is not None:
                try:
                    device.close()
                except Exception:
                    pass


class HeadsetBatteryProvider:
    """Prefer the GX03 receiver, falling back only when it is absent."""

    def __init__(
        self,
        stop_event=None,
        gx03_provider=None,
        bluetooth_provider=None,
    ):
        self.gx03 = gx03_provider or Gx03HidBatteryProvider(stop_event)
        self.bluetooth = bluetooth_provider or WindowsBluetoothBatteryProvider()

    def query(self):
        gx03_outcome = self.gx03.query()
        if gx03_outcome.succeeded or gx03_outcome.interface_present is not False:
            return gx03_outcome
        return self.bluetooth.query()


class BatteryAgent:
    """Low-frequency, reconnect-friendly peripheral battery collector."""

    def __init__(self, on_headset_state_change=None, enable_headset=True):
        self.lock = threading.Lock()
        self._on_headset_state_change = on_headset_state_change
        self._enable_headset = bool(enable_headset)
        self._states: Dict[str, BatteryState] = {
            key: BatteryState() for key in BATTERY_KEYS
        }

        self.wake_event = threading.Event()
        self.stop_event = threading.Event()
        self.thread = None

        self._headset_provider = (
            HeadsetBatteryProvider(self.stop_event)
            if self._enable_headset else None
        )
        self._headset_query_lock = threading.Lock()
        self._headset_thread = None

        self._status_lock = threading.Lock()
        self._last_errors = {}
        self._last_error_log_times = {}

        self._last_input_tick = None
        self._last_input_resume_at = None

    @property
    def cache(self):
        """Compatibility snapshot for older diagnostics; use get_data()."""
        return self.get_data()

    def start(self):
        """Start exactly one scheduler thread."""
        if self.thread and self.thread.is_alive():
            return

        self.stop_event.clear()
        self.wake_event.clear()
        self.thread = threading.Thread(
            target=self._loop,
            name="BatteryAgent",
            daemon=True,
        )
        self.thread.start()

        if not hid:
            self._record_failure("HID", "hidapi is not installed")

    def stop(self):
        """Wake and stop the scheduler without waiting for its next interval."""
        self.stop_event.set()
        self.wake_event.set()

        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=2.0)
        if self.thread and self.thread.is_alive():
            logger.warning("[Battery] scheduler did not stop within 2 seconds")
        else:
            self.thread = None

        headset_thread = self._headset_thread
        if headset_thread and headset_thread.is_alive():
            headset_thread.join(timeout=1.0)

    def trigger_instant_update(self):
        self.wake_event.set()

    def get_data(self):
        """Return the unchanged legacy telemetry protocol."""
        with self.lock:
            data = {
                key: self._states[key].sample.display_value()
                for key in BATTERY_KEYS
            }
            headset = self._states["hp_battery"].sample
            data["hp_case_battery"] = (
                "%d%%" % headset.case_percent
                if headset.online and headset.case_percent is not None
                else "OFF"
            )
            return data

    def get_status(self):
        """Return structured diagnostic state without changing telemetry."""
        now = time.monotonic()
        with self.lock:
            result = {}
            for key, state in self._states.items():
                result[key] = {
                    "value": state.sample.display_value(),
                    "percent": state.sample.percent,
                    "case_percent": state.sample.case_percent,
                    "charging": state.sample.charging,
                    "online": state.sample.online,
                    "stale": state.stale,
                    "consecutive_failures": state.consecutive_failures,
                    "error_code": state.error_code,
                    "error": state.error,
                    "interface_present": state.interface_present,
                    "last_attempt_age_seconds": (
                        None if state.last_attempt_at is None
                        else max(0.0, now - state.last_attempt_at)
                    ),
                    "last_success_age_seconds": (
                        None if state.last_success_at is None
                        else max(0.0, now - state.last_success_at)
                    ),
                }
            return result

    def _loop(self):
        # Establish the input baseline before the initial HID queries. If a
        # sleeping device wakes on the user's next action, the changed Windows
        # timestamp can bring its next query forward immediately.
        self._last_input_tick = self._read_last_input_tick()
        self._active_sync_all()
        now = time.monotonic()
        next_due = {
            key: (
                now + self._poll_interval(key)
                if self._enable_headset or key != "hp_battery"
                else float("inf")
            )
            for key in BATTERY_KEYS
        }

        while not self.stop_event.is_set():
            if self.wake_event.is_set():
                self.wake_event.clear()
                if self.stop_event.is_set():
                    break

                self._active_sync_all()
                now = time.monotonic()
                next_due = {
                    key: (
                        now + self._poll_interval(key)
                        if self._enable_headset or key != "hp_battery"
                        else float("inf")
                    )
                    for key in BATTERY_KEYS
                }

            resume_idle = self._consume_input_resume_hint()
            if resume_idle is not None:
                refresh_at = time.monotonic() + INPUT_RESUME_SETTLE_SECONDS
                for key in INPUT_RESUME_KEYS:
                    next_due[key] = refresh_at
                logger.info(
                    "[Battery] input resumed after %.0f seconds; "
                    "keyboard/mouse refresh scheduled",
                    resume_idle,
                )

            for key in HID_POLL_ORDER:
                if self.stop_event.is_set():
                    break
                if time.monotonic() < next_due[key]:
                    continue

                if self._should_defer_hid_query(key):
                    # Do not touch sleeping keyboard/mouse receivers merely to
                    # discover that the peripheral is asleep. Keep the due time
                    # at infinity while the system remains idle; a qualified
                    # input-resume hint above is the only normal way to schedule
                    # the next query. This avoids periodic HID traffic keeping
                    # wireless firmware awake overnight.
                    next_due[key] = float("inf")
                    continue

                outcome = self._query_hid_spec(HID_DEVICE_SPECS[key])
                self._apply_outcome(key, outcome)
                next_due[key] = time.monotonic() + self._poll_interval(key)

            if (
                not self.stop_event.is_set()
                and time.monotonic() >= next_due["hp_battery"]
            ):
                self._start_headset_query()
                next_due["hp_battery"] = (
                    time.monotonic() + self._poll_interval("hp_battery")
                )

            next_wakeup = min(next_due.values())
            wait_time = max(0.0, min(1.0, next_wakeup - time.monotonic()))
            self.wake_event.wait(wait_time)

    def _poll_interval(self, key):
        with self.lock:
            state = self._states[key]
            failures = state.consecutive_failures
            stale = state.stale
            charging = state.sample.charging
            online = state.sample.online

        # Headset presence can change while it is sitting in the charging
        # case.  Keep probing it at the short interval even when the initial
        # state is the default offline state, or after a confirmed disconnect.
        # This also avoids scheduling the very first asynchronous query five
        # minutes into the future before its first result has arrived.
        if key == "hp_battery":
            return HEADSET_POLL_INTERVAL

        if failures:
            if stale:
                retry_index = min(failures - 1, len(TRANSIENT_RETRY_INTERVALS) - 1)
                return TRANSIENT_RETRY_INTERVALS[retry_index]
            return FAILED_RETRY_INTERVAL

        if not online:
            return OFFLINE_RETRY_INTERVAL

        spec = HID_DEVICE_SPECS[key]
        if charging and spec.charging_interval is not None:
            return spec.charging_interval
        return spec.normal_interval

    @staticmethod
    def _read_last_input_tick():
        """Return Windows' 32-bit last-input tick without opening any device."""
        if os.name != "nt":
            return None

        try:
            import ctypes
            from ctypes import wintypes

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = (
                    ("cbSize", wintypes.UINT),
                    ("dwTime", wintypes.DWORD),
                )

            info = LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(info)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                return None
            return int(info.dwTime)
        except Exception:
            return None

    @staticmethod
    def _idle_seconds_from_ticks(last_input_tick, current_tick):
        """Calculate Windows idle time with 32-bit tick-wrap handling."""
        return (
            ((int(current_tick) & 0xFFFFFFFF)
             - (int(last_input_tick) & 0xFFFFFFFF))
            & 0xFFFFFFFF
        ) / 1000.0

    @classmethod
    def _read_system_idle_seconds(cls):
        """Return current Windows input-idle time without opening HID devices."""
        if os.name != "nt":
            return None

        last_input_tick = cls._read_last_input_tick()
        if last_input_tick is None:
            return None

        try:
            import ctypes

            current_tick = int(ctypes.windll.kernel32.GetTickCount64())
            return cls._idle_seconds_from_ticks(last_input_tick, current_tick)
        except Exception:
            return None

    def _should_defer_hid_query(self, key, idle_seconds=None):
        """Protect sleeping keyboard/mouse firmware from active polling."""
        if key not in IDLE_HID_GUARDED_KEYS:
            return False
        if idle_seconds is None:
            idle_seconds = self._read_system_idle_seconds()
        return (
            idle_seconds is not None
            and idle_seconds >= IDLE_HID_SUPPRESS_SECONDS
        )

    def _consume_input_resume_hint(self, input_tick=None, now=None):
        """Return the preceding idle seconds once for a qualified input resume.

        GetLastInputInfo reports accepted system input, not a persistent device
        connection. A large gap between two observed input timestamps is a good
        low-cost hint that a wireless keyboard or mouse may just have woken.
        """
        if input_tick is None:
            input_tick = self._read_last_input_tick()
        if input_tick is None:
            return None

        input_tick = int(input_tick) & 0xFFFFFFFF
        previous_tick = self._last_input_tick
        self._last_input_tick = input_tick
        if previous_tick is None or input_tick == previous_tick:
            return None

        idle_seconds = (
            (input_tick - int(previous_tick)) & 0xFFFFFFFF
        ) / 1000.0
        if idle_seconds < INPUT_RESUME_IDLE_SECONDS:
            return None

        if now is None:
            now = time.monotonic()
        if (
            self._last_input_resume_at is not None
            and now - self._last_input_resume_at < INPUT_RESUME_COOLDOWN_SECONDS
        ):
            return None

        self._last_input_resume_at = now
        return idle_seconds

    def _start_headset_query(self):
        if (
            not self._enable_headset
            or self.stop_event.is_set()
            or self._headset_query_lock.locked()
        ):
            return
        if self._headset_thread and self._headset_thread.is_alive():
            return

        self._headset_thread = threading.Thread(
            target=self._query_headset_async,
            name="HeadsetBatteryQuery",
            daemon=True,
        )
        self._headset_thread.start()

    def _query_headset_async(self):
        if not self._headset_query_lock.acquire(blocking=False):
            return
        try:
            outcome = self._query_headset_outcome()
            if not self.stop_event.is_set():
                self._apply_outcome("hp_battery", outcome)
        except Exception as exc:
            reason = "%s: %s" % (type(exc).__name__, exc)
            self._record_failure("Headset", reason)
            self._apply_outcome(
                "hp_battery",
                QueryOutcome(
                    error_code="provider_error",
                    error=reason,
                    interface_present=None,
                ),
            )
        finally:
            self._headset_query_lock.release()

    def _active_sync_all(self):
        """Refresh peripherals without bypassing the idle-sleep guard.

        This path is also used by periodic framebuffer refreshes. Keyboard and
        mouse vendor HID queries must obey the same idle protection as the
        normal scheduler; otherwise a screen refresh could keep a wireless
        peripheral awake while the user is idle.
        """
        for key in HID_POLL_ORDER:
            if self.stop_event.is_set():
                return
            if self._should_defer_hid_query(key):
                continue
            outcome = self._query_hid_spec(HID_DEVICE_SPECS[key])
            self._apply_outcome(key, outcome)
        if self._enable_headset:
            self._start_headset_query()

    def _apply_outcome(self, key, outcome, now=None):
        """Commit a query result while preserving last-known-good data."""
        if outcome.error_code == "cancelled":
            return

        if now is None:
            now = time.monotonic()

        state_change = None
        with self.lock:
            state = self._states[key]
            previous_online = state.sample.online
            state.last_attempt_at = now
            state.interface_present = outcome.interface_present

            if outcome.succeeded:
                state.sample = outcome.sample
                state.last_success_at = now
                state.consecutive_failures = 0
                state.stale = False
                state.error_code = None
                state.error = None
            else:
                state.consecutive_failures += 1
                state.error_code = outcome.error_code
                state.error = outcome.error

                confirmed_absent = outcome.interface_present is False
                can_keep_last_good = (
                    state.sample.online
                    and state.last_success_at is not None
                    and not confirmed_absent
                    and state.consecutive_failures < TRANSIENT_FAILURE_LIMIT
                )

                if can_keep_last_good:
                    state.stale = True
                else:
                    state.sample = BatterySample()
                    state.stale = False

            if key == "hp_battery" and state.sample.online != previous_online:
                state_change = state.sample.online

        # Do not run an external process while holding the battery-state lock.
        if state_change is not None and self._on_headset_state_change:
            try:
                self._on_headset_state_change(state_change)
            except Exception as exc:
                logger.warning(
                    "[Battery] headset state callback failed: %s: %s",
                    type(exc).__name__,
                    exc,
                )

    def _record_failure(self, device_name, reason):
        now = time.monotonic()
        with self._status_lock:
            previous = self._last_errors.get(device_name)
            last_log = self._last_error_log_times.get(device_name, 0.0)
            self._last_errors[device_name] = reason
            should_log = previous != reason or now - last_log >= ERROR_LOG_INTERVAL
            if should_log:
                self._last_error_log_times[device_name] = now

        if should_log:
            logger.warning("[Battery][%s] %s", device_name, reason)

    def _record_success(self, device_name):
        with self._status_lock:
            previous = self._last_errors.pop(device_name, None)
            self._last_error_log_times.pop(device_name, None)
        if previous is not None:
            logger.info("[Battery][%s] query recovered", device_name)

    def _failure_outcome(
        self,
        device_name,
        error_code,
        reason,
        interface_present=None,
    ):
        self._record_failure(device_name, reason)
        return QueryOutcome(
            error_code=error_code,
            error=reason,
            interface_present=interface_present,
        )

    @staticmethod
    def _drain_input(device):
        """Discard a bounded number of stale reports; never spin forever."""
        for _ in range(HID_DRAIN_LIMIT):
            if not device.read(64):
                break

    def _run_hid_query(
        self,
        device_name,
        vid,
        pid,
        usage_page,
        command,
        parser: Callable[[Sequence[int]], Optional[BatterySample]],
    ):
        if not hid:
            return self._failure_outcome(
                device_name,
                "hid_unavailable",
                "hidapi is not installed",
                interface_present=None,
            )

        try:
            path = find_hid_interface(vid, pid, usage_page)
        except HidEnumerationError as exc:
            return self._failure_outcome(
                device_name,
                "enumeration_failed",
                str(exc),
                interface_present=None,
            )

        if not path:
            return self._failure_outcome(
                device_name,
                "not_found",
                "vendor HID interface not found",
                interface_present=False,
            )

        device = None
        try:
            device = hid.device()
            device.open_path(path)
            device.set_nonblocking(True)
            self._drain_input(device)

            written = device.write(command)
            if written is not None and written <= 0:
                raise OSError("HID write returned no data")

            deadline = time.monotonic() + HID_QUERY_TIMEOUT
            next_retry = time.monotonic() + HID_RETRY_INTERVAL

            while time.monotonic() < deadline and not self.stop_event.is_set():
                data = device.read(64)
                if data:
                    sample = parser(data)
                    if sample is not None:
                        self._record_success(device_name)
                        return QueryOutcome(
                            sample=sample,
                            interface_present=True,
                        )

                now = time.monotonic()
                if now >= next_retry:
                    written = device.write(command)
                    if written is not None and written <= 0:
                        raise OSError("HID retry write returned no data")
                    next_retry = now + HID_RETRY_INTERVAL

                self.stop_event.wait(HID_READ_INTERVAL)

            if self.stop_event.is_set():
                return QueryOutcome(error_code="cancelled")

            return self._failure_outcome(
                device_name,
                "timeout",
                "response timeout after %.0f ms" % (HID_QUERY_TIMEOUT * 1000),
                interface_present=True,
            )
        except Exception as exc:
            reason = "%s: %s" % (type(exc).__name__, exc)
            return self._failure_outcome(
                device_name,
                "io_error",
                reason,
                interface_present=True,
            )
        finally:
            if device is not None:
                try:
                    device.close()
                except Exception:
                    pass

    @staticmethod
    def _parse_mouse_report(data):
        if len(data) >= 7 and data[0] == 65 and data[2] == 140:
            battery = data[6]
            charge_flag = data[5]
            if 0 <= battery <= 100:
                return BatterySample(
                    percent=battery,
                    charging=(charge_flag == 1),
                    online=True,
                )
        if len(data) >= 1 and data[0] == 95:
            return BatterySample(percent=None, charging=True, online=True)
        return None

    @staticmethod
    def _parse_keyboard_report(data):
        if len(data) < 8:
            return None
        if data[0] != KEYBOARD_REPORT_ID or data[1] != KEYBOARD_STATUS_ID:
            return None

        battery = data[KEYBOARD_BATTERY_OFFSET]
        status = data[KEYBOARD_STATUS_OFFSET]
        if not 0 <= battery <= 100:
            return None
        if status == 16:
            return BatterySample(percent=battery, charging=True, online=True)
        if status == 1:
            return BatterySample(percent=battery, charging=False, online=True)
        if status == 0:
            return BatterySample()
        return None

    @staticmethod
    def _parse_controller_report(data):
        if len(data) <= CONTROLLER_BATTERY_OFFSET:
            return None
        if data[0] != CONTROLLER_REPORT_ID:
            return None

        battery = data[CONTROLLER_BATTERY_OFFSET]
        charge = data[CONTROLLER_CHARGE_OFFSET]
        if 0 <= battery <= 100:
            return BatterySample(
                percent=battery,
                charging=(charge == 1),
                online=True,
            )
        return None

    def _query_hid_spec(self, spec):
        parser = getattr(self, spec.parser_name)
        return self._run_hid_query(
            spec.name,
            spec.vid,
            spec.pid,
            spec.usage_page,
            spec.command,
            parser,
        )

    @staticmethod
    def _legacy_outcome_value(outcome):
        if outcome.succeeded:
            return outcome.sample.display_value()
        return "OFF"

    # Preserve the original private diagnostic helpers' string return values.
    # The scheduler itself uses _query_hid_spec() and structured outcomes.
    def _query_ms_direct(self):
        return self._legacy_outcome_value(
            self._query_hid_spec(HID_DEVICE_SPECS["ms_battery"])
        )

    def _query_kb_direct(self):
        return self._legacy_outcome_value(
            self._query_hid_spec(HID_DEVICE_SPECS["kb_battery"])
        )

    def _query_gp_direct(self):
        return self._legacy_outcome_value(
            self._query_hid_spec(HID_DEVICE_SPECS["gp_battery"])
        )

    @staticmethod
    def _registry_battery_value(value):
        """Compatibility wrapper for older isolated tests."""
        return WindowsBluetoothBatteryProvider._registry_battery_value(value)

    def _query_headset_outcome(self):
        if not self._enable_headset or self._headset_provider is None:
            return QueryOutcome(
                error_code="disabled",
                error="headset collection delegated to HeadsetMonitor",
                interface_present=None,
            )
        outcome = self._headset_provider.query()
        if outcome.succeeded:
            self._record_success("Headset")
        elif outcome.error_code != "cancelled":
            self._record_failure(
                "Headset",
                outcome.error or outcome.error_code or "query failed",
            )
        return outcome

    def _query_headset_direct(self):
        return self._legacy_outcome_value(self._query_headset_outcome())

    # Compatibility aliases for older local diagnostics.
    def _query_bt_outcome(self):
        return self._query_headset_outcome()

    def _query_bt_direct(self):
        return self._query_headset_direct()
