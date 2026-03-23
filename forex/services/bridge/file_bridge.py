"""
forex/services/bridge/file_bridge.py
─────────────────────────────────────
Python-side of the file-based IPC bridge between Linux and MT5 (Bottles/Wine).

How the bridge works end-to-end
─────────────────────────────────
1. Python creates a TradeCommand object and serialises it to JSON.
2. Python writes that JSON to  <bridge_dir>/commands/cmd_<uuid>.json
3. The LykhanBridge MQL5 EA polls the commands/ folder every 500ms.
4. The EA reads the command, executes on Deriv, writes result to
   <bridge_dir>/results/res_<uuid>.json
5. Python polls results/ until it finds the matching file, parses it,
   and returns it to the caller. Both sides delete their files after.

Special directories:
  status/  — GET_STATUS responses (AccountSnapshot shape)
  results/ — GET_CANDLES + trade results (TradeResult or CandleData shape)

The heartbeat.txt file is written by the EA every 5 seconds. Python checks
this to determine if the EA is alive before attempting any commands.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Optional

from forex.services.core.schemas import (
    AccountSnapshot,
    CandleData,
    TradeAction,
    TradeCommand,
    TradeResult,
    TradeStatus,
)
from forex.services.config.settings import forex_settings


class BridgeError(Exception):
    """Raised when the bridge fails to communicate with MT5."""


class FileBridge:
    """
    Manages the file-based IPC channel between the Python agent and the
    LykhanBridge Expert Advisor running inside MT5 in Bottles/Wine.

    Single responsibility: move data reliably between Python and MT5 via
    the shared filesystem. All trading logic lives in TradeExecutor.
    """

    def __init__(
        self,
        bridge_dir:       Optional[Path] = None,
        timeout:          Optional[int]  = None,
        poll_interval_ms: Optional[int]  = None,
    ) -> None:
        self._bridge_dir    = Path(bridge_dir or forex_settings.mt5_bridge_dir)
        self._timeout       = timeout or forex_settings.bridge_timeout_seconds
        self._poll_interval = (poll_interval_ms or forex_settings.bridge_poll_interval_ms) / 1000.0

        self._cmd_dir    = self._bridge_dir / "commands"
        self._result_dir = self._bridge_dir / "results"
        self._status_dir = self._bridge_dir / "status"

        self._initialise_directories()

    # ── Public API ────────────────────────────────────────────────────────────

    def send_command(self, command: TradeCommand) -> TradeResult:
        """
        Write a command file and block until the EA writes back a TradeResult.
        Raises BridgeError if no result arrives within the timeout window.
        """
        cmd_file = self._cmd_dir    / f"cmd_{command.command_id}.json"
        res_file = self._result_dir / f"res_{command.command_id}.json"

        # Compact JSON — no spaces after colons/commas — required by MQL5 parser
        import json
        payload = json.dumps(
            json.loads(command.model_dump_json()),
            separators=(',', ':')
        )
        cmd_file.write_text(payload, encoding="utf-8")

        result = self._poll_for_result(res_file, command.command_id)

        self._safe_delete(cmd_file)
        self._safe_delete(res_file)
        return result

    def get_account_status(self) -> AccountSnapshot:
        """
        Send a GET_STATUS command and wait for the EA to write back a
        full AccountSnapshot into the status/ sub-directory.
        """
        cmd         = TradeCommand(action=TradeAction.GET_STATUS)
        cmd_file    = self._cmd_dir    / f"cmd_{cmd.command_id}.json"
        status_file = self._status_dir / f"status_{cmd.command_id}.json"

        cmd_file.write_text(cmd.model_dump_json(), encoding="utf-8")

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

    def get_candles(
        self,
        symbol:    str,
        timeframe: str,
        count:     int = 100,
    ) -> CandleData:
        """
        Request OHLCV candle data from MT5 via the file bridge.

        The EA receives a GET_CANDLES command, calls CopyRates(), and writes
        the result JSON to results/res_<uuid>.json. Python parses it as
        CandleData and returns to the caller.

        :param symbol:    Trading symbol, e.g. "EURUSD"
        :param timeframe: Timeframe string, e.g. "M1", "M5", "H1", "D1", "W1"
        :param count:     Number of bars to return (most recent N bars)
        :raises BridgeError: if no result arrives within the timeout window.
        """
        cmd_id   = str(uuid.uuid4())
        cmd_file = self._cmd_dir    / f"cmd_{cmd_id}.json"
        res_file = self._result_dir / f"res_{cmd_id}.json"

        # Write the candles command manually (not using TradeCommand schema
        # since GET_CANDLES has non-trade fields: timeframe and count)
        payload = json.dumps({
            "command_id": cmd_id,
            "action":     "GET_CANDLES",
            "symbol":     symbol,
            "timeframe":  timeframe,
            "count":      count,
        }, separators=(',', ':'))
        cmd_file.write_text(payload, encoding="utf-8")

        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if res_file.exists():
                try:
                    raw = res_file.read_text(encoding="utf-8")
                    self._safe_delete(cmd_file)
                    self._safe_delete(res_file)
                    return CandleData.model_validate_json(raw)
                except Exception as exc:
                    raise BridgeError(
                        f"Failed to parse candle data for {symbol} {timeframe}: {exc}"
                    ) from exc
            time.sleep(self._poll_interval)

        self._safe_delete(cmd_file)
        raise BridgeError(
            f"Timeout ({self._timeout}s) waiting for candle data "
            f"{symbol} {timeframe}. Is the LykhanBridge EA running?"
        )

    def is_bridge_alive(self) -> bool:
        """
        Check whether the MT5 EA is alive by inspecting the heartbeat file.
        Returns True only if the file exists AND was modified within 10 seconds.
        """
        heartbeat = self._bridge_dir / "heartbeat.txt"
        if not heartbeat.exists():
            return False
        return (time.time() - heartbeat.stat().st_mtime) < 10

    # ── Private Helpers ───────────────────────────────────────────────────────

    def _initialise_directories(self) -> None:
        for d in (self._cmd_dir, self._result_dir, self._status_dir):
            d.mkdir(parents=True, exist_ok=True)

    def _poll_for_result(self, res_file: Path, command_id: str) -> TradeResult:
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if res_file.exists():
                try:
                    raw = res_file.read_text(encoding="utf-8")
                    import logging
                    logging.getLogger(__name__).info("BRIDGE RAW RESULT: %s", raw)
                    return TradeResult.model_validate_json(raw)
                except Exception as exc:
                    raise BridgeError(
                        f"Failed to parse result for command {command_id[:8]}: {exc}"
                    ) from exc
            time.sleep(self._poll_interval)

        raise BridgeError(
            f"Timeout ({self._timeout}s) waiting for result of command "
            f"{command_id[:8]}. Ensure LykhanBridge EA is attached and "
            "Algo Trading is enabled."
        )

    @staticmethod
    def _safe_delete(path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass