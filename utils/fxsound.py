# -*- coding: utf-8 -*-
"""Reversible FxSound routing for the GX03 headset.

FxSound ships ``fxmcp.exe``, an official local MCP server.  This module uses
that supported interface instead of editing FxSound's files or relying on the
old command-line switches, which are not reliable on current FxSound builds.
Each state transition starts a short-lived MCP session, reads the real current
state, and applies output + preset atomically.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

from utils.logger import logger


FXMCP_EXE = os.environ.get(
    "FXMCP_EXE",
    r"C:\Program Files\FxSound LLC\FxSound\fxmcp.exe",
)
# Kept as a compatibility alias for callers that imported the old constant.
FXSOUND_EXE = FXMCP_EXE
# Kept for compatibility with older configuration; MCP status is authoritative.
FXSOUND_STATUS = os.environ.get(
    "FXSOUND_STATUS",
    os.path.expandvars(r"%APPDATA%\FxSound\status.json"),
)
GX03_OUTPUT_NAME = os.environ.get(
    "FXSOUND_HEADSET_OUTPUT",
    "扬声器 (HECATE GX03 Ultra)",
)
GX03_PRESET = os.environ.get("FXSOUND_HEADSET_PRESET", "fps")
MCP_PROTOCOL_VERSION = "2024-11-05"
COMMAND_TIMEOUT_SECONDS = 5.0


class FxSoundController:
    """Switch FxSound to the headset profile on headset state transitions."""

    def __init__(
        self,
        executable: str = FXMCP_EXE,
        status_path: str = FXSOUND_STATUS,
        headset_output: str = GX03_OUTPUT_NAME,
        headset_preset: str = GX03_PRESET,
        runner=None,
        process_factory: Optional[Callable[..., Any]] = None,
        command_timeout: float = COMMAND_TIMEOUT_SECONDS,
    ):
        # ``executable`` remains the first positional argument for compatibility,
        # but it now must point to the official fxmcp.exe binary.
        self.executable = executable
        self.status_path = status_path  # legacy, intentionally not read
        self.headset_output = headset_output
        self.headset_preset = headset_preset
        # ``runner`` was used by the old CLI implementation.  Accepting it and
        # rejecting it clearly avoids silently running a wrong fake in callers.
        if runner is not None and process_factory is None:
            process_factory = runner
        self._process_factory = process_factory or subprocess.Popen
        self._command_timeout = float(command_timeout)
        self._lock = threading.Lock()
        self._restore_target: Optional[Tuple[str, str]] = None
        self._headset_mode = False
        self._request_id = 0

    @property
    def headset_mode(self):
        return self._headset_mode

    def on_headset_state_change(self, online: bool):
        """Apply one edge-triggered transition; repeated states are ignored."""
        with self._lock:
            if online == self._headset_mode:
                return False
            if online:
                changed = self._switch_to_headset()
                if changed:
                    self._headset_mode = True
                return changed

            changed = self._restore_previous_output()
            if changed:
                self._headset_mode = False
            return changed

    def _switch_to_headset(self) -> bool:
        status = self._call_tool("fxsound_get_status", {})
        if status is None:
            logger.warning("[FxSound] MCP status query failed; headset switch skipped")
            return False

        current_output = self._canonical_output_name(status)
        current_preset = status.get("selected_preset")
        if not current_output or current_preset is None:
            logger.warning("[FxSound] MCP status has no current output/preset")
            return False

        # Preserve the user's current selection exactly; do not assume that
        # Realtek/minemine is always the desired return target.
        self._restore_target = (current_output, str(current_preset))
        if not self._apply(self.headset_output, self.headset_preset):
            self._restore_target = None
            return False

        logger.info(
            "[FxSound] headset online: output=%s preset=%s",
            self.headset_output,
            self.headset_preset,
        )
        return True

    def _restore_previous_output(self) -> bool:
        target = self._restore_target
        if target is None:
            # Nothing was changed by this process instance.
            return False

        output, preset = target
        if not self._apply(output, preset):
            return False

        logger.info(
            "[FxSound] headset offline: restored output=%s preset=%s",
            output,
            preset,
        )
        self._restore_target = None
        return True

    def _apply(self, output: str, preset: str) -> bool:
        payload = self._call_tool(
            "fxsound_apply_settings",
            {"output_device": output, "preset": preset},
        )
        if not isinstance(payload, dict) or payload.get("applied") is not True:
            logger.warning("[FxSound] MCP apply failed: %s", payload)
            return False
        return True

    def _call_tool(self, tool_name: str, arguments: Dict[str, Any]):
        """Run one official MCP request and return its structured payload."""
        if not os.path.isfile(self.executable):
            logger.warning("[FxSound] MCP executable not found: %s", self.executable)
            return None

        process = None
        response_queue: "queue.Queue[str]" = queue.Queue()
        try:
            process = self._process_factory(
                [self.executable],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if process.stdin is None or process.stdout is None:
                raise OSError("fxmcp pipes were not created")

            def read_stdout():
                try:
                    for line in process.stdout:
                        response_queue.put(line)
                except (OSError, ValueError):
                    # The process can close its pipe while a timeout is being
                    # handled.  The caller will report the actual failure.
                    return

            reader = threading.Thread(target=read_stdout, name="fxmcp-reader", daemon=True)
            reader.start()

            initialize_id = self._next_request_id()
            self._send(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": initialize_id,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "miku-cyber-screen", "version": "1"},
                    },
                },
            )
            initialize_response = self._wait_for_response(
                process, response_queue, initialize_id
            )
            if initialize_response is None:
                return None
            if "error" in initialize_response:
                logger.warning("[FxSound] MCP initialize error: %s", initialize_response["error"])
                return None

            self._send(
                process,
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                },
            )
            request_id = self._next_request_id()
            self._send(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                },
            )
            response = self._wait_for_response(process, response_queue, request_id)
            if response is None:
                return None
            if "error" in response:
                logger.warning("[FxSound] MCP tool error (%s): %s", tool_name, response["error"])
                return None
            return self._extract_tool_payload(response.get("result"))
        except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
            logger.warning("[FxSound] MCP request failed (%s): %s", tool_name, exc)
            return None
        finally:
            if process is not None:
                self._close_process(process)

    def _send(self, process, message: Dict[str, Any]) -> None:
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.flush()

    def _wait_for_response(self, process, response_queue, request_id):
        deadline = time.monotonic() + self._command_timeout
        while time.monotonic() < deadline:
            try:
                line = response_queue.get(timeout=min(0.1, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            try:
                message = json.loads(line)
            except (TypeError, ValueError):
                logger.debug("[FxSound] ignoring non-JSON MCP output: %r", line)
                continue
            if message.get("id") == request_id:
                return message
        logger.warning("[FxSound] MCP response timeout (request id %s)", request_id)
        return None

    @staticmethod
    def _extract_tool_payload(result):
        if not isinstance(result, dict) or result.get("isError"):
            return None
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        for item in result.get("content", []):
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            try:
                decoded = json.loads(item.get("text", ""))
            except (TypeError, ValueError):
                continue
            if isinstance(decoded, dict):
                return decoded
        return None

    @staticmethod
    def _canonical_output_name(status: Dict[str, Any]) -> Optional[str]:
        selected = status.get("selected_output")
        if not isinstance(selected, str) or not selected.strip():
            return None
        selected = selected.strip()
        devices = status.get("output_devices")
        if not isinstance(devices, list):
            return selected
        device_names = [str(device) for device in devices]
        if selected in device_names:
            return selected
        # FxSound reports selected_output without the Windows friendly-name
        # wrapper, while apply_settings expects the complete device name.
        for device in device_names:
            if device.startswith("扬声器 (") and device.endswith(")"):
                inner_name = device[len("扬声器 (") : -1]
                if inner_name == selected:
                    return device
        for device in device_names:
            if device.endswith(selected):
                return device
        logger.warning(
            "[FxSound] selected output %r was not found in output_devices; using it as-is",
            selected,
        )
        return selected

    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id

    @staticmethod
    def _close_process(process) -> None:
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.terminate()
            process.wait(timeout=0.5)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except (OSError, ValueError):
                pass


__all__ = [
    "FXMCP_EXE",
    "FxSoundController",
    "GX03_OUTPUT_NAME",
    "GX03_PRESET",
]
