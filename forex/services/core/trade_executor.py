"""
forex/services/core/trade_executor.py
──────────────────────────────────────
High-level trade execution engine — the main entry point for anything
that wants to interact with MT5 from the Python side.

Responsibilities
─────────────────
- Validate trade parameters before they ever reach the bridge
- Build TradeCommand objects with correct defaults from settings
- Delegate execution to FileBridge (which handles the file I/O)
- Return structured TradeResult objects to the caller

What this class deliberately does NOT do
──────────────────────────────────────────
- It knows nothing about files, JSON, or the bridge directory structure.
  That is entirely FileBridge's responsibility.
- It knows nothing about Django, HTTP requests, or Celery tasks.
  Those layers sit above this one and call into it.

This clean separation means that when we later add a TCP socket bridge
(for a Windows VPS deployment), we only swap out FileBridge — this
class stays exactly the same. That is the Dependency Inversion
Principle in practice.
"""
from __future__ import annotations

from typing import Optional

from forex.services.bridge.file_bridge import FileBridge, BridgeError
from forex.services.config.settings import forex_settings
from forex.services.core.schemas import (
    AccountSnapshot,
    TradeAction,
    TradeCommand,
    TradeResult,
    TradeStatus,
)


class TradeValidationError(Exception):
    """
    Raised when a trade request fails pre-execution validation.
    This is a deliberate early failure — we catch the problem in Python
    before wasting a round-trip to the MT5 bridge.
    """


class TradeExecutor:
    """
    Orchestrates the full lifecycle of a trade:
        validate → build command → execute via bridge → return result.

    The constructor accepts an optional FileBridge instance, which makes
    it easy to inject a mock bridge during testing without needing to
    have MT5 running at all. If no bridge is provided, a real one is
    created automatically using the settings from your .env file.
    """

    def __init__(self, bridge: Optional[FileBridge] = None) -> None:
        self._bridge   = bridge or FileBridge()
        self._symbol   = forex_settings.default_symbol
        self._lot      = forex_settings.default_lot_size
        self._sl_pips  = forex_settings.default_sl_pips
        self._tp_pips  = forex_settings.default_tp_pips
        self._magic    = forex_settings.default_magic_number
        self._slippage = forex_settings.default_slippage

    # ── Public Trade API ──────────────────────────────────────────────────────

    def open_buy(
        self,
        symbol:   Optional[str]   = None,
        lot_size: Optional[float] = None,
        sl_pips:  Optional[int]   = None,
        tp_pips:  Optional[int]   = None,
        comment:  str             = "lykhan-buy",
    ) -> TradeResult:
        """
        Open a market BUY (long) position.
        Any parameter left as None falls back to the default from your .env file.
        """
        return self._open_trade(
            action   = TradeAction.BUY,
            symbol   = symbol   or self._symbol,
            lot_size = lot_size or self._lot,
            sl_pips  = sl_pips  if sl_pips is not None else self._sl_pips,
            tp_pips  = tp_pips  if tp_pips is not None else self._tp_pips,
            comment  = comment,
        )

    def open_sell(
        self,
        symbol:   Optional[str]   = None,
        lot_size: Optional[float] = None,
        sl_pips:  Optional[int]   = None,
        tp_pips:  Optional[int]   = None,
        comment:  str             = "lykhan-sell",
    ) -> TradeResult:
        """
        Open a market SELL (short) position.
        Any parameter left as None falls back to the default from your .env file.
        """
        return self._open_trade(
            action   = TradeAction.SELL,
            symbol   = symbol   or self._symbol,
            lot_size = lot_size or self._lot,
            sl_pips  = sl_pips  if sl_pips is not None else self._sl_pips,
            tp_pips  = tp_pips  if tp_pips is not None else self._tp_pips,
            comment  = comment,
        )

    def close_trade(self, ticket: int) -> TradeResult:
        """
        Close one specific open position identified by its MT5 ticket number.
        The ticket number is visible in MT5's Trade tab and is also returned
        in the TradeResult when you originally opened the position.
        """
        cmd = TradeCommand(
            action   = TradeAction.CLOSE,
            ticket   = ticket,
            symbol   = self._symbol,
            lot_size = self._lot,
            magic    = self._magic,
            slippage = self._slippage,
        )
        return self._execute(cmd)

    def close_all_trades(self) -> TradeResult:
        """
        Close every open position that carries our magic number.
        This is a nuclear option — use with care. The CLI will prompt
        for confirmation before calling this.
        """
        cmd = TradeCommand(
            action = TradeAction.CLOSE_ALL,
            symbol = self._symbol,
            magic  = self._magic,
        )
        return self._execute(cmd)

    def get_account_snapshot(self) -> AccountSnapshot:
        """
        Fetch a live AccountSnapshot from MT5 — balance, equity, margin,
        and all currently open positions with their floating P&L.
        """
        return self._bridge.get_account_status()

    def check_bridge(self) -> bool:
        """
        Returns True if the MT5 EA bridge is alive based on the heartbeat file.
        Use this as a health check before attempting any trade operations.
        """
        return self._bridge.is_bridge_alive()

    # ── Private Helpers ───────────────────────────────────────────────────────

    def _open_trade(
        self,
        action:   TradeAction,
        symbol:   str,
        lot_size: float,
        sl_pips:  int,
        tp_pips:  int,
        comment:  str,
    ) -> TradeResult:
        """Validate parameters, build the command, and execute it."""
        self._validate_open_trade(symbol, lot_size)

        cmd = TradeCommand(
            action   = action,
            symbol   = symbol,
            lot_size = lot_size,
            sl_pips  = sl_pips,
            tp_pips  = tp_pips,
            slippage = self._slippage,
            magic    = self._magic,
            comment  = comment,
        )
        return self._execute(cmd)

    def _execute(self, cmd: TradeCommand) -> TradeResult:
        """
        Send a command to the bridge and handle any bridge-level errors
        by converting them into a TradeResult with ERROR status rather
        than letting exceptions bubble up to the caller unchecked.
        """
        try:
            return self._bridge.send_command(cmd)
        except BridgeError as exc:
            # Return a structured error result so the caller always gets
            # a TradeResult regardless of what went wrong — no surprises.
            return TradeResult(
                command_id    = cmd.command_id,
                status        = TradeStatus.ERROR,
                error_message = str(exc),
            )

    def _validate_open_trade(self, symbol: str, lot_size: float) -> None:
        """
        Catch obviously invalid parameters before they reach the bridge.
        These checks are fast, free, and catch the most common mistakes.
        """
        if not symbol:
            raise TradeValidationError("Symbol cannot be empty.")
        if lot_size < 0.01:
            raise TradeValidationError(
                f"Lot size {lot_size} is below the minimum allowed (0.01)."
            )
        if lot_size > 5000.0:
            raise TradeValidationError(
                f"Lot size {lot_size} exceeds the maximum allowed (100.0)."
            )