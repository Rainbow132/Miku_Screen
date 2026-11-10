# -*- coding: utf-8 -*-
import json
import os
import queue
import tempfile
import unittest

from utils.fxsound import FxSoundController


class FakeStdout:
    def __init__(self):
        self._queue = queue.Queue()

    def push(self, message):
        self._queue.put(json.dumps(message, ensure_ascii=False) + "\n")

    def close(self):
        self._queue.put(None)

    def __iter__(self):
        return self

    def __next__(self):
        line = self._queue.get()
        if line is None:
            raise StopIteration
        return line


class FakeStdin:
    def __init__(self, process):
        self.process = process
        self.closed = False

    def write(self, text):
        self.process.messages.append(json.loads(text))
        self.process.handle(json.loads(text))

    def flush(self):
        return None

    def close(self):
        self.closed = True


class FakeProcess:
    def __init__(self, status, apply_result_provider=None, malformed=False):
        self.status = status
        self.apply_result_provider = apply_result_provider or (lambda: True)
        self.malformed = malformed
        self.stdout = FakeStdout()
        self.stdin = FakeStdin(self)
        self.messages = []
        self.returncode = None

    def handle(self, message):
        method = message.get("method")
        request_id = message.get("id")
        if method == "initialize":
            self.stdout.push(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"protocolVersion": "2024-11-05", "capabilities": {}},
                }
            )
        elif method == "tools/call":
            tool = message.get("params", {}).get("name")
            if self.malformed:
                # This still exercises the JSON parser's safe failure path.
                self.stdout._queue.put("not-json\n")
                return
            if tool == "fxsound_get_status":
                payload = self.status
            elif tool == "fxsound_apply_settings":
                payload = {"applied": self.apply_result_provider()}
            else:
                payload = {}
            self.stdout.push(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "structuredContent": payload,
                        "content": [{"type": "text", "text": json.dumps(payload)}],
                    },
                }
            )

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0
        self.stdout.close()

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):
        self.returncode = -9
        self.stdout.close()


class FakeProcessFactory:
    def __init__(self, statuses=None, apply_results=None, malformed=False):
        self.statuses = list(statuses or [])
        self.apply_results = list(apply_results or [])
        self.malformed = malformed
        self.processes = []

    def __call__(self, args, **kwargs):
        status = self.statuses.pop(0) if self.statuses else {}
        def next_apply_result():
            return self.apply_results.pop(0) if self.apply_results else True
        process = FakeProcess(status, next_apply_result, self.malformed)
        self.processes.append(process)
        return process


class FxSoundControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.exe = os.path.join(self.tmp.name, "fxmcp.exe")
        with open(self.exe, "wb") as stream:
            stream.write(b"test")

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _status(selected_output="Realtek(R) Audio", selected_preset="minemine"):
        return {
            "selected_output": selected_output,
            "selected_preset": selected_preset,
            "output_devices": [
                "扬声器 (网易虚拟音频设备)",
                "扬声器 (HECATE GX03 Ultra)",
                "扬声器 (Realtek(R) Audio)",
            ],
        }

    def test_online_switches_and_offline_restores_canonical_selection(self):
        factory = FakeProcessFactory(
            statuses=[self._status()],
            apply_results=[True, True],
        )
        controller = FxSoundController(
            executable=self.exe,
            process_factory=factory,
        )

        self.assertTrue(controller.on_headset_state_change(True))
        self.assertTrue(controller.headset_mode)
        self.assertTrue(controller.on_headset_state_change(False))
        self.assertFalse(controller.headset_mode)

        apply_calls = [
            message
            for process in factory.processes
            for message in process.messages
            if message.get("method") == "tools/call"
            and message.get("params", {}).get("name") == "fxsound_apply_settings"
        ]
        self.assertEqual(
            [call["params"]["arguments"] for call in apply_calls],
            [
                {"output_device": "扬声器 (HECATE GX03 Ultra)", "preset": "fps"},
                {"output_device": "扬声器 (Realtek(R) Audio)", "preset": "minemine"},
            ],
        )

    def test_short_selected_output_is_mapped_to_full_friendly_name(self):
        factory = FakeProcessFactory(
            statuses=[self._status(selected_output="HECATE GX03 Ultra")],
        )
        controller = FxSoundController(executable=self.exe, process_factory=factory)
        self.assertTrue(controller.on_headset_state_change(True))
        self.assertEqual(
            controller._restore_target,
            ("扬声器 (HECATE GX03 Ultra)", "minemine"),
        )

    def test_duplicate_state_is_ignored(self):
        factory = FakeProcessFactory(statuses=[self._status()])
        controller = FxSoundController(executable=self.exe, process_factory=factory)
        self.assertTrue(controller.on_headset_state_change(True))
        self.assertFalse(controller.on_headset_state_change(True))
        self.assertEqual(len(factory.processes), 2)  # status + one apply

    def test_apply_false_does_not_enter_headset_mode(self):
        factory = FakeProcessFactory(
            statuses=[self._status()],
            apply_results=[False],
        )
        controller = FxSoundController(executable=self.exe, process_factory=factory)
        self.assertFalse(controller.on_headset_state_change(True))
        self.assertFalse(controller.headset_mode)
        self.assertIsNone(controller._restore_target)

    def test_mcp_malformed_json_fails_safely(self):
        factory = FakeProcessFactory(statuses=[self._status()], malformed=True)
        controller = FxSoundController(
            executable=self.exe,
            process_factory=factory,
            command_timeout=0.05,
        )
        self.assertFalse(controller.on_headset_state_change(True))
        self.assertFalse(controller.headset_mode)

    def test_missing_mcp_executable_fails_safely(self):
        controller = FxSoundController(
            executable=os.path.join(self.tmp.name, "missing-fxmcp.exe")
        )
        self.assertFalse(controller.on_headset_state_change(True))


if __name__ == "__main__":
    unittest.main()
