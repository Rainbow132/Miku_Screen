# -*- coding: utf-8 -*-

import unittest

import utils.battery as battery


class BatteryProtocolTests(unittest.TestCase):
    def test_case_battery_low_threshold(self):
        self.assertTrue(battery.is_case_battery_low("24%"))
        self.assertTrue(battery.is_case_battery_low("0%"))
        self.assertFalse(battery.is_case_battery_low("25%"))
        self.assertFalse(battery.is_case_battery_low("37%"))
        self.assertFalse(battery.is_case_battery_low("OFF"))
        self.assertFalse(battery.is_case_battery_low("101%"))
        self.assertFalse(battery.is_case_battery_low("unknown"))

    def test_protocol_constants_remain_unchanged(self):
        self.assertEqual(battery.CONTROLLER_VID, 0x3537)
        self.assertEqual(battery.CONTROLLER_PID, 0x100B)
        self.assertEqual(battery.CONTROLLER_USAGE_PAGE, 65520)
        self.assertEqual(
            battery.CONTROLLER_WAKEUP_CMD,
            [0x0F, 0x04, 0x00, 0x41] + [0x00] * 60,
        )
        self.assertEqual(battery.CONTROLLER_REPORT_ID, 0x12)
        self.assertEqual(battery.CONTROLLER_BATTERY_OFFSET, 36)
        self.assertEqual(battery.CONTROLLER_CHARGE_OFFSET, 35)

        self.assertEqual(battery.MOUSE_VID, 0x1915)
        self.assertEqual(battery.MOUSE_PID, 0x0723)
        self.assertEqual(battery.MOUSE_USAGE_PAGE, 65290)
        self.assertEqual(
            battery.MOUSE_QUERY_CMD_HIGH_HZ,
            [0x00, 65, 0, 129, 1] + [0] * 58 + [222],
        )

        self.assertEqual(battery.KEYBOARD_VID, 0x3554)
        self.assertEqual(battery.KEYBOARD_PID, 0xFA09)
        self.assertEqual(battery.KEYBOARD_USAGE_PAGE, 65282)
        self.assertEqual(
            battery.KEYBOARD_QUERY_CMD,
            [0x13, 0x05, 0x01] + [0] * 16 + [0x19],
        )
        self.assertEqual(battery.KEYBOARD_REPORT_ID, 0x13)
        self.assertEqual(battery.KEYBOARD_STATUS_ID, 0x0A)
        self.assertEqual(battery.KEYBOARD_BATTERY_OFFSET, 6)
        self.assertEqual(battery.KEYBOARD_STATUS_OFFSET, 7)

        self.assertEqual(battery.GX03_VID, 0x35BB)
        self.assertEqual(battery.GX03_PID, 0xA217)
        self.assertEqual(battery.GX03_USAGE_PAGE, 0xFF02)
        self.assertEqual(battery.GX03_PATH_MARKER, "MI_04&COL03")
        self.assertEqual(
            battery.GX03_QUERY_CMD,
            [0xD0, 0x35, 0xBB, 0x01, 0x00],
        )

    def test_legacy_display_format_is_unchanged(self):
        self.assertEqual(battery.BatterySample().display_value(), "OFF")
        self.assertEqual(
            battery.BatterySample(percent=85, online=True).display_value(),
            "85%",
        )
        self.assertEqual(
            battery.BatterySample(
                percent=85, charging=True, online=True
            ).display_value(),
            "+85%",
        )
        self.assertEqual(
            battery.BatterySample(
                percent=None, charging=True, online=True
            ).display_value(),
            "+CHG",
        )

    def test_mouse_parser(self):
        report = [65, 0, 140, 0, 0, 1, 73]
        sample = battery.BatteryAgent._parse_mouse_report(report)
        self.assertEqual(sample.display_value(), "+73%")

        sample = battery.BatteryAgent._parse_mouse_report([95])
        self.assertEqual(sample.display_value(), "+CHG")
        self.assertIsNone(battery.BatteryAgent._parse_mouse_report([1, 2]))

    def test_keyboard_parser(self):
        report = [0x13, 0x0A, 0, 0, 0, 0, 64, 16]
        self.assertEqual(
            battery.BatteryAgent._parse_keyboard_report(report).display_value(),
            "+64%",
        )
        report[7] = 1
        self.assertEqual(
            battery.BatteryAgent._parse_keyboard_report(report).display_value(),
            "64%",
        )
        report[7] = 0
        self.assertEqual(
            battery.BatteryAgent._parse_keyboard_report(report).display_value(),
            "OFF",
        )

    def test_controller_parser(self):
        report = [0] * 37
        report[0] = 0x12
        report[35] = 1
        report[36] = 42
        self.assertEqual(
            battery.BatteryAgent._parse_controller_report(report).display_value(),
            "+42%",
        )


class BatteryStateTests(unittest.TestCase):
    def setUp(self):
        self.agent = battery.BatteryAgent()

    def _success(self, key="kb_battery", value=80, charging=False, now=10.0):
        self.agent._apply_outcome(
            key,
            battery.QueryOutcome(
                sample=battery.BatterySample(
                    percent=value,
                    charging=charging,
                    online=True,
                ),
                interface_present=True,
            ),
            now=now,
        )

    def _timeout(self, key="kb_battery", now=20.0):
        self.agent._apply_outcome(
            key,
            battery.QueryOutcome(
                error_code="timeout",
                error="response timeout",
                interface_present=True,
            ),
            now=now,
        )

    def test_two_transient_failures_keep_last_known_good(self):
        self._success()
        self._timeout(now=20.0)
        self.assertEqual(self.agent.get_data()["kb_battery"], "80%")
        self.assertTrue(self.agent.get_status()["kb_battery"]["stale"])
        self.assertEqual(self.agent._poll_interval("kb_battery"), 15.0)

        self._timeout(now=30.0)
        status = self.agent.get_status()["kb_battery"]
        self.assertEqual(status["value"], "80%")
        self.assertTrue(status["stale"])
        self.assertEqual(status["consecutive_failures"], 2)
        self.assertEqual(self.agent._poll_interval("kb_battery"), 30.0)

    def test_third_transient_failure_becomes_off(self):
        self._success()
        self._timeout(now=20.0)
        self._timeout(now=30.0)
        self._timeout(now=40.0)

        status = self.agent.get_status()["kb_battery"]
        self.assertEqual(status["value"], "OFF")
        self.assertFalse(status["stale"])
        self.assertEqual(status["consecutive_failures"], 3)
        self.assertEqual(
            self.agent._poll_interval("kb_battery"),
            battery.FAILED_RETRY_INTERVAL,
        )

    def test_confirmed_absent_interface_becomes_off_immediately(self):
        self._success()
        self.agent._apply_outcome(
            "kb_battery",
            battery.QueryOutcome(
                error_code="not_found",
                error="vendor HID interface not found",
                interface_present=False,
            ),
            now=20.0,
        )
        status = self.agent.get_status()["kb_battery"]
        self.assertEqual(status["value"], "OFF")
        self.assertFalse(status["stale"])

    def test_successful_recovery_clears_failure_state(self):
        self._success(value=80)
        self._timeout()
        self._success(value=79, charging=True, now=30.0)

        status = self.agent.get_status()["kb_battery"]
        self.assertEqual(status["value"], "+79%")
        self.assertFalse(status["stale"])
        self.assertEqual(status["consecutive_failures"], 0)
        self.assertIsNone(status["error_code"])
        self.assertEqual(self.agent._poll_interval("kb_battery"), 120.0)

    def test_get_data_adds_separate_case_key_without_repurposing_headset(self):
        data = self.agent.get_data()
        self.assertEqual(data["hp_battery"], "OFF")
        self.assertEqual(data["hp_case_battery"], "OFF")
        self.assertEqual(
            {key: data[key] for key in battery.BATTERY_KEYS},
            {
                "kb_battery": "OFF",
                "ms_battery": "OFF",
                "gp_battery": "OFF",
                "hp_battery": "OFF",
            },
        )

    def test_input_resume_hint_ignores_short_idle_gap(self):
        self.agent._last_input_tick = 1000
        self.assertIsNone(
            self.agent._consume_input_resume_hint(
                input_tick=1000 + int(
                    (battery.INPUT_RESUME_IDLE_SECONDS - 1) * 1000
                ),
                now=100.0,
            )
        )

    def test_input_resume_hint_fires_once_after_long_idle_gap(self):
        self.agent._last_input_tick = 1000
        idle_ms = int((battery.INPUT_RESUME_IDLE_SECONDS + 5) * 1000)
        idle = self.agent._consume_input_resume_hint(
            input_tick=1000 + idle_ms,
            now=100.0,
        )
        self.assertAlmostEqual(
            idle,
            battery.INPUT_RESUME_IDLE_SECONDS + 5,
        )

        # Another long gap inside the cooldown is deliberately coalesced.
        self.assertIsNone(
            self.agent._consume_input_resume_hint(
                input_tick=1000 + idle_ms * 2,
                now=100.0 + battery.INPUT_RESUME_COOLDOWN_SECONDS - 1,
            )
        )

    def test_input_resume_hint_handles_32_bit_tick_wrap(self):
        self.agent._last_input_tick = 0xFFFFFF00
        idle_ms = int((battery.INPUT_RESUME_IDLE_SECONDS + 1) * 1000)
        wrapped_tick = (0xFFFFFF00 + idle_ms) & 0xFFFFFFFF
        idle = self.agent._consume_input_resume_hint(
            input_tick=wrapped_tick,
            now=200.0,
        )
        self.assertAlmostEqual(
            idle,
            battery.INPUT_RESUME_IDLE_SECONDS + 1,
        )

    def test_idle_guard_defers_keyboard_and_mouse_only(self):
        idle = battery.IDLE_HID_SUPPRESS_SECONDS
        self.assertTrue(
            self.agent._should_defer_hid_query("kb_battery", idle)
        )
        self.assertTrue(
            self.agent._should_defer_hid_query("ms_battery", idle)
        )
        self.assertFalse(
            self.agent._should_defer_hid_query("gp_battery", idle)
        )

    def test_idle_guard_allows_query_while_user_is_active(self):
        idle = battery.IDLE_HID_SUPPRESS_SECONDS - 0.001
        self.assertFalse(
            self.agent._should_defer_hid_query("kb_battery", idle)
        )

    def test_current_idle_calculation_handles_32_bit_tick_wrap(self):
        self.assertAlmostEqual(
            self.agent._idle_seconds_from_ticks(0xFFFFFF00, 0x00000300),
            1.024,
        )

    def test_headset_initial_offline_is_polled_quickly(self):
        # The headset query is asynchronous.  Its initial default state must
        # not cause the first real probe to be delayed by OFFLINE_RETRY_INTERVAL.
        self.assertEqual(
            self.agent._poll_interval("hp_battery"),
            battery.HEADSET_POLL_INTERVAL,
        )

    def test_low_frequency_intervals_after_confirmed_sleep(self):
        self.assertEqual(
            battery.HID_DEVICE_SPECS["ms_battery"].normal_interval,
            300.0,
        )
        self.assertEqual(
            self.agent._poll_interval("kb_battery"),
            battery.OFFLINE_RETRY_INTERVAL,
        )
        self.assertEqual(
            self.agent._poll_interval("ms_battery"),
            battery.OFFLINE_RETRY_INTERVAL,
        )


class FakeDevice:
    def __init__(self, reports):
        self.reports = list(reports)
        self.writes = []
        self.opened_path = None
        self.nonblocking = None
        self.closed = False

    def open_path(self, path):
        self.opened_path = path

    def set_nonblocking(self, value):
        self.nonblocking = value

    def read(self, _size):
        if self.reports:
            return self.reports.pop(0)
        return []

    def write(self, command):
        self.writes.append(list(command))
        return len(command)

    def close(self):
        self.closed = True


class FakeHid:
    def __init__(self, fake_device=None, enumerate_error=None, devices=None):
        self.fake_device = fake_device
        self.enumerate_error = enumerate_error
        self.devices = devices

    def enumerate(self, _vid, _pid):
        if self.enumerate_error:
            raise self.enumerate_error
        if self.devices is not None:
            return self.devices
        return [{"path": b"vendor-path", "usage_page": battery.MOUSE_USAGE_PAGE}]

    def device(self):
        return self.fake_device


class HidTransportTests(unittest.TestCase):
    def setUp(self):
        self.original_hid = battery.hid

    def tearDown(self):
        battery.hid = self.original_hid

    def test_query_opens_writes_parses_and_closes(self):
        # First empty report ends the stale drain; the next one is the reply.
        device = FakeDevice([[], [65, 0, 140, 0, 0, 0, 91]])
        battery.hid = FakeHid(device)
        agent = battery.BatteryAgent()

        outcome = agent._query_hid_spec(
            battery.HID_DEVICE_SPECS["ms_battery"]
        )

        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.sample.display_value(), "91%")
        self.assertEqual(device.opened_path, b"vendor-path")
        self.assertTrue(device.nonblocking)
        self.assertEqual(device.writes[0], battery.MOUSE_QUERY_CMD_HIGH_HZ)
        self.assertTrue(device.closed)

    def test_direct_helper_keeps_legacy_string_result(self):
        device = FakeDevice([[], [65, 0, 140, 0, 0, 1, 88]])
        battery.hid = FakeHid(device)
        agent = battery.BatteryAgent()
        self.assertEqual(agent._query_ms_direct(), "+88%")

    def test_enumeration_failure_is_not_reported_as_not_found(self):
        battery.hid = FakeHid(enumerate_error=OSError("enumeration broke"))
        agent = battery.BatteryAgent()

        outcome = agent._query_hid_spec(
            battery.HID_DEVICE_SPECS["ms_battery"]
        )

        self.assertEqual(outcome.error_code, "enumeration_failed")
        self.assertIsNone(outcome.interface_present)
        self.assertIn("enumeration broke", outcome.error)

    def test_bounded_stale_report_drain(self):
        class NeverEmptyDevice:
            def __init__(self):
                self.read_count = 0

            def read(self, _size):
                self.read_count += 1
                return [1]

        device = NeverEmptyDevice()
        battery.BatteryAgent._drain_input(device)
        self.assertEqual(device.read_count, battery.HID_DRAIN_LIMIT)

    def test_registry_battery_conversion(self):
        self.assertEqual(battery.BatteryAgent._registry_battery_value(77), 77)
        self.assertEqual(battery.BatteryAgent._registry_battery_value(b"\x32"), 50)
        self.assertIsNone(battery.BatteryAgent._registry_battery_value(101))


class StaticProvider:
    def __init__(self, outcome):
        self.outcome = outcome
        self.query_count = 0

    def query(self):
        self.query_count += 1
        return self.outcome


class Gx03BatteryTests(unittest.TestCase):
    def setUp(self):
        self.original_hid = battery.hid

    def tearDown(self):
        battery.hid = self.original_hid

    @staticmethod
    def _gx03_devices():
        return [
            {
                "path": b"HID#VID_35BB&PID_A217&MI_04&Col02#control",
                "usage_page": battery.GX03_USAGE_PAGE,
            },
            {
                "path": b"HID#VID_35BB&PID_A217&MI_04&Col03#battery",
                "usage_page": battery.GX03_USAGE_PAGE,
            },
            {
                "path": b"HID#VID_35BB&PID_A217&MI_04&Col04#other",
                "usage_page": battery.GX03_USAGE_PAGE,
            },
        ]

    def test_interface_selection_requires_col03_path_marker(self):
        battery.hid = FakeHid(devices=self._gx03_devices())
        self.assertEqual(
            battery.find_gx03_interface(),
            b"HID#VID_35BB&PID_A217&MI_04&Col03#battery",
        )

    def test_query_parses_official_battery_response_and_closes(self):
        response = [
            0xD0, 0x35, 0xBB, 0x09, 0x00, 0x03,
            100, 100, 0x00, 0x01, 0x27, 0x01, 0x1D,
        ]
        device = FakeDevice([[], response])
        battery.hid = FakeHid(
            fake_device=device,
            devices=self._gx03_devices(),
        )

        outcome = battery.Gx03HidBatteryProvider().query()

        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.sample.display_value(), "100%")
        self.assertEqual(outcome.sample.case_percent, 0)
        self.assertEqual(
            device.opened_path,
            b"HID#VID_35BB&PID_A217&MI_04&Col03#battery",
        )
        self.assertEqual(device.writes, [battery.GX03_QUERY_CMD])
        self.assertTrue(device.closed)

    def test_parser_reads_case_battery_without_changing_earbud_value(self):
        response = [0xD0, 0x35, 0xBB, 0x09, 0x00, 0x03, 80, 60, 24]
        sample = battery.Gx03HidBatteryProvider._parse_report(response)
        self.assertEqual(sample.percent, 60)
        self.assertEqual(sample.case_percent, 24)
        self.assertEqual(sample.display_value(), "60%")
        self.assertTrue(sample.online)
        self.assertFalse(sample.charging)

    def test_parser_ignores_missing_or_invalid_case_value(self):
        short = [0xD0, 0x35, 0xBB, 0x09, 0x00, 0x03, 80, 60]
        invalid = short + [101]
        self.assertIsNone(
            battery.Gx03HidBatteryProvider._parse_report(short).case_percent
        )
        self.assertIsNone(
            battery.Gx03HidBatteryProvider._parse_report(invalid).case_percent
        )

    def test_parser_rejects_wrong_header_opcode_and_offline_status(self):
        good = [0xD0, 0x35, 0xBB, 0x09, 0x00, 0x03, 80, 60]
        for index, bad_value in ((0, 0x00), (4, 0x02), (5, 0x00)):
            report = list(good)
            report[index] = bad_value
            self.assertIsNone(
                battery.Gx03HidBatteryProvider._parse_report(report)
            )

    def test_provider_closes_device_after_io_failure(self):
        class FailingDevice(FakeDevice):
            def write(self, _command):
                raise OSError("write failed")

        device = FailingDevice([[]])
        battery.hid = FakeHid(
            fake_device=device,
            devices=self._gx03_devices(),
        )

        outcome = battery.Gx03HidBatteryProvider().query()

        self.assertEqual(outcome.error_code, "io_error")
        self.assertTrue(outcome.interface_present)
        self.assertTrue(device.closed)

    def test_headset_provider_falls_back_when_gx03_is_absent(self):
        gx03 = StaticProvider(
            battery.QueryOutcome(
                error_code="not_found",
                interface_present=False,
            )
        )
        bluetooth = StaticProvider(
            battery.QueryOutcome(
                sample=battery.BatterySample(percent=77, online=True),
                interface_present=True,
            )
        )
        provider = battery.HeadsetBatteryProvider(
            gx03_provider=gx03,
            bluetooth_provider=bluetooth,
        )

        outcome = provider.query()

        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.sample.percent, 77)
        self.assertEqual(gx03.query_count, 1)
        self.assertEqual(bluetooth.query_count, 1)

    def test_headset_provider_does_not_mask_present_gx03_timeout(self):
        gx03 = StaticProvider(
            battery.QueryOutcome(
                error_code="timeout",
                error="response timeout",
                interface_present=True,
            )
        )
        bluetooth = StaticProvider(
            battery.QueryOutcome(
                sample=battery.BatterySample(percent=77, online=True),
                interface_present=True,
            )
        )
        provider = battery.HeadsetBatteryProvider(
            gx03_provider=gx03,
            bluetooth_provider=bluetooth,
        )

        outcome = provider.query()

        self.assertEqual(outcome.error_code, "timeout")
        self.assertEqual(bluetooth.query_count, 0)


if __name__ == "__main__":
    unittest.main()

class HeadsetDecouplingTests(unittest.TestCase):
    def test_disabled_headset_collection_does_not_create_provider_or_query(self):
        agent = battery.BatteryAgent(enable_headset=False)
        self.assertFalse(agent._enable_headset)
        self.assertIsNone(agent._headset_provider)

        agent._start_headset_query()
        self.assertIsNone(agent._headset_thread)

        outcome = agent._query_headset_outcome()
        self.assertEqual(outcome.error_code, "disabled")

    def test_default_battery_agent_keeps_legacy_headset_collection(self):
        agent = battery.BatteryAgent()
        self.assertTrue(agent._enable_headset)
        self.assertIsNotNone(agent._headset_provider)

