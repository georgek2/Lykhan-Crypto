"""
forex/services/core/account_monitor.py
───────────────────────────────────────
Background account monitor that periodically fetches live account data
from MT5 via the TradeExecutor and makes it available to the rest of
the application.

This module is designed to run as a background daemon thread alongside
the main application — it never blocks the caller and never crashes the
parent process even if MT5 goes offline temporarily. Errors are caught,
logged, and retried on the next polling cycle.

Future extensions (Phase 2 and beyond)
────────────────────────────────────────
- Drawdown guard: automatically call close_all_trades() if floating
  loss exceeds a configured percentage of the account balance.
- Alert system: send a notification (email, Telegram, Slack) when
  equity drops below a threshold or a large profit is achieved.
- Django signal integration: emit a Django signal on each snapshot so
  the dashboard can update in real time via WebSockets.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from forex.services.core.schemas import AccountSnapshot
from forex.services.core.trade_executor import TradeExecutor


class AccountMonitor:
    """
    Periodically fetches an AccountSnapshot from MT5 and exposes it via
    the `latest` property. Optionally calls a user-supplied callback
    function every time a new snapshot arrives.

    Usage — one-shot fetch (synchronous, no thread):
        monitor = AccountMonitor()
        snapshot = monitor.fetch_once()
        print(snapshot.balance)

    Usage — continuous background polling:
        def on_update(snapshot):
            print(f"Equity: {snapshot.equity}")

        monitor = AccountMonitor(on_update=on_update, poll_interval_seconds=10)
        monitor.start()
        # ... rest of your application runs here ...
        monitor.stop()
    """

    def __init__(
        self,
        executor: Optional[TradeExecutor] = None,
        poll_interval_seconds: int | None = None,
        on_update: Optional[Callable[[AccountSnapshot], None]] = None,
    ) -> None:
        # Accept an injected executor for testability, or create a real one
        self._executor  = executor or TradeExecutor()
        from forex.services.config.settings import forex_settings
        # Use passed interval if provided, otherwise fall back to configured default
        self._interval  = poll_interval_seconds if poll_interval_seconds is not None else forex_settings.monitor_poll_interval_seconds
        self._on_update = on_update

        self._latest_snapshot: Optional[AccountSnapshot] = None
        self._running = False
        self._thread:  Optional[threading.Thread] = None

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def latest(self) -> Optional[AccountSnapshot]:
        """
        The most recently fetched AccountSnapshot, or None if fetch_once()
        or start() has not been called yet. Reading this property is always
        safe — it never blocks and never raises.
        """
        return self._latest_snapshot

    # ── Public API ────────────────────────────────────────────────────────────

    def fetch_once(self) -> AccountSnapshot:
        """
        Perform a single synchronous snapshot fetch. Does not start a
        background thread. Useful for one-off status checks and for the
        CLI's `status` and `monitor` commands.
        """
        snapshot = self._executor.get_account_snapshot()
        self._latest_snapshot = snapshot
        if self._on_update:
            self._on_update(snapshot)
        return snapshot

    def start(self) -> None:
        """
        Start the background polling thread. The thread is a daemon thread,
        meaning it will be killed automatically if the main process exits —
        you don't need to call stop() for the process to terminate cleanly.
        """
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target = self._loop,
            daemon = True,
            name   = "LykhanAccountMonitor",
        )
        self._thread.start()

    def stop(self) -> None:
        """
        Signal the background thread to stop and wait up to 5 seconds for
        it to finish its current polling cycle before returning.
        """
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        """
        The background thread's main loop. Runs until self._running is False.
        Errors are swallowed per iteration so a single MT5 disconnection
        doesn't kill the monitor permanently — it just waits and retries.
        """
        while self._running:
            try:
                snapshot = self._executor.get_account_snapshot()
                self._latest_snapshot = snapshot
                if self._on_update:
                    self._on_update(snapshot)
            except Exception:
                # Don't crash the monitor thread on a transient error.
                # The next iteration will retry automatically.
                pass
            time.sleep(self._interval)