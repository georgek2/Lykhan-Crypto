"""
forex/services/bridge/file_bridge.py
─────────────────────────────────────
Python-side of the file-based IPC bridge between Linux and MT5 (Bottles/Wine).

How the bridge works end-to-end
─────────────────────────────────
1. Python creates a TradeCommand object and serialises it to JSON.
2. Python writes that JSON to  <bridge_dir>/commands/cmd_<uuid>.json
3. The LykhanBridge MQL5 EA polls the commands/ folder every 500ms.
4. The EA reads the command file, executes the trade on Deriv, then
   writes the result to  <bridge_dir>/results/res_<uuid>.json
5. Python polls the results/ folder until it finds the matching file,
   deserialises it into a TradeResult, and returns it to the caller.
6. Both sides delete their files after processing to keep things clean.

For GET_STATUS commands, the EA writes to the status/ sub-directory
instead of results/, because a status response is a different shape
(AccountSnapshot) from a trade result (TradeResult).

The heartbeat.txt file is written by the EA every 5 seconds. Python
checks whether that file exists and is recently modified to determine
if the EA is alive and running before attempting any commands.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from forex.services.core.schemas import (
    TradeCommand,
    TradeResult,
    TradeStatus,
    AccountSnapshot,
)
from forex.services.config.settings import forex_settings


class BridgeError(Exception):
    """
    Raised when the bridge fails to communicate with MT5.
    This could mean the EA isn't running, the bridge directory doesn't
    exist, or a result file couldn't be read or parsed.
    """


class FileBridge:
    """
    Manages the file-based IPC channel between the Python agent and the
    LykhanBridge Expert Advisor running inside MT5 in Bottles/Wine.

    This class has a single, well-defined responsibility: moving data
    reliably between the Python process and the MT5 EA via the shared
    file system. It knows nothing about trading logic — that belongs in
    TradeExecutor. The separation means we could swap this file bridge
    for a TCP socket bridge later without touching any trading logic.
    """

    def __init__(
        self,
        bridge_dir: Optional[Path] = None,
        timeout: Optional[int] = None,
        poll_interval_ms: Optional[int] = None,
    ) -> None:
        self._bridge_dir    = Path(bridge_dir or forex_settings.mt5_bridge_dir)
        self._timeout       = timeout or forex_settings.bridge_timeout_seconds
        self._poll_interval = (poll_interval_ms or forex_settings.bridge_poll_interval_ms) / 1000.0

        # The three sub-directories the bridge uses, mirroring the structure
        # the EA creates on its side inside MQL5\Files\mt5bridge\
        self._cmd_dir    = self._bridge_dir / "commands"
        self._result_dir = self._bridge_dir / "results"
        self._status_dir = self._bridge_dir / "status"

        self._initialise_directories()

    # ── Public API ────────────────────────────────────────────────────────────

    def send_command(self, command: TradeCommand) -> TradeResult:
        """
        Write a command file to the bridge and block until the EA writes
        back a result, then return the parsed TradeResult.

        The blocking behaviour is intentional — trades are sequential
        operations and we need to know the outcome before proceeding.
        The timeout prevents the caller from waiting forever if the EA
        goes offline mid-operation.

        :raises BridgeError: if no result arrives within the timeout window.
        """
        cmd_file = self._cmd_dir  / f"cmd_{command.command_id}.json"
        res_file = self._result_dir / f"res_{command.command_id}.json"

        # Write the command as a pretty-printed JSON file so it's easy
        # to inspect manually during development and debugging
        cmd_file.write_text(command.model_dump_json(), encoding="utf-8")

        # Block here until the EA writes the result file back
        result = self._poll_for_result(res_file, command.command_id)

        # Clean up both files — the EA may have already deleted the command
        # file, but we try anyway. The result file is ours to clean up.
        self._safe_delete(cmd_file)
        self._safe_delete(res_file)

        return result

    def get_account_status(self) -> AccountSnapshot:
        """
        Send a GET_STATUS command and wait for the EA to write back a
        full AccountSnapshot JSON file into the status/ sub-directory.
        """
        from forex.services.core.schemas import TradeAction
        cmd         = TradeCommand(action=TradeAction.GET_STATUS)
        cmd_file    = self._cmd_dir    / f"cmd_{cmd.command_id}.json"
        status_file = self._status_dir / f"status_{cmd.command_id}.json"

        cmd_file.write_text(cmd.model_dump_json(), encoding="utf-8")

        # Poll the status directory rather than the results directory
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if status_file.exists():
                raw = status_file.read_text(encoding="utf-8")
                self._safe_delete(cmd_file)
                self._safe_delete(status_file)
                return AccountSnapshot.model_validate_json(raw)
            time.sleep(self._poll_interval)

        raise BridgeError(
            f"Timeout ({self._timeout}s) waiting for account status. "
            "Is the LykhanBridge EA attached and running in MT5?"
        )

    def is_bridge_alive(self) -> bool:
        """
        Check whether the MT5 EA is currently running by inspecting the
        heartbeat file it writes every 5 seconds.

        Returns True only if the file exists AND was modified within the
        last 10 seconds — a stale file from a previous session doesn't count.
        """
        heartbeat = self._bridge_dir / "heartbeat.txt"
        if not heartbeat.exists():
            return False
        age = time.time() - heartbeat.stat().st_mtime
        return age < 10

    # ── Private Helpers ───────────────────────────────────────────────────────

    def _initialise_directories(self) -> None:
        """Create the bridge sub-directories on the Python side if missing."""
        for directory in (self._cmd_dir, self._result_dir, self._status_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def _poll_for_result(self, res_file: Path, command_id: str) -> TradeResult:
        """
        Busy-wait until the EA writes a result file for this command_id,
        then parse and return it as a TradeResult.
        """
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if res_file.exists():
                try:
                    raw    = res_file.read_text(encoding="utf-8")
                    return TradeResult.model_validate_json(raw)
                except Exception as exc:
                    raise BridgeError(
                        f"Failed to parse result file for command {command_id[:8]}: {exc}"
                    ) from exc
            time.sleep(self._poll_interval)

        raise BridgeError(
            f"Timeout ({self._timeout}s) waiting for result of command "
            f"{command_id[:8]}. Ensure the LykhanBridge EA is attached "
            "and running in MT5 with Algo Trading enabled."
        )

    @staticmethod
    def _safe_delete(path: Path) -> None:
        """Delete a file silently — don't raise if it's already gone."""
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass  # File already deleted by the EA or a previous call — that's fine