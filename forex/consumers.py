"""
forex/consumers.py
───────────────────
Django Channels WebSocket consumer for the Lykhan live dashboard.

On connect:
  - Adds client to the "lykhan_dashboard" channel group
  - Sends an initial payload: account snapshot + recent trades + current bias

On group messages (sent by Celery tasks):
  - "bias.update"  → forwards new session bias to browser
  - "trade.event"  → forwards trade open/close to browser
  - "account.tick" → forwards account snapshot to browser

The dashboard JavaScript receives these and updates the UI in real time
without polling. The Celery tasks call _broadcast_* helpers to push
events into the group.

The consumer also handles a periodic account tick via a background
coroutine that polls MT5 every 10 seconds while clients are connected.
"""
from __future__ import annotations

import asyncio
import json
import logging

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async

logger = logging.getLogger(__name__)

DASHBOARD_GROUP = "lykhan_dashboard"
ACCOUNT_TICK_INTERVAL = 10   # seconds between MT5 account snapshots


class DashboardConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for the live trading dashboard.

    URL: ws://<host>/ws/dashboard/
    """

    async def connect(self) -> None:
        await self.channel_layer.group_add(DASHBOARD_GROUP, self.channel_name)
        await self.accept()
        logger.info("DashboardConsumer: client connected")

        # Send initial state immediately so the page isn't blank
        await self._send_initial_state()

        # Start periodic account tick in background
        self._tick_task = asyncio.ensure_future(self._account_tick_loop())

    async def disconnect(self, close_code: int) -> None:
        if hasattr(self, "_tick_task"):
            self._tick_task.cancel()
        await self.channel_layer.group_discard(DASHBOARD_GROUP, self.channel_name)
        logger.info("DashboardConsumer: client disconnected (code=%d)", close_code)

    # ── Group message handlers (type maps to method name with . → _) ──────────

    async def bias_update(self, event: dict) -> None:
        """Receives bias.update from channel group, forwards to browser."""
        await self.send(text_data=json.dumps({
            "event":      "bias_update",
            "symbol":     event["symbol"],
            "bias":       event["bias"],
            "confidence": event["confidence"],
            "reasoning":  event["reasoning"],
            "model_used": event["model_used"],
        }))

    async def trade_event(self, event: dict) -> None:
        """Receives trade.event from channel group, forwards to browser."""
        await self.send(text_data=json.dumps({
            "event":   "trade_event",
            "payload": event["payload"],
        }))

    async def account_tick(self, event: dict) -> None:
        """Receives account.tick from channel group, forwards to browser."""
        await self.send(text_data=json.dumps({
            "event":   "account_tick",
            "payload": event["payload"],
        }))

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _send_initial_state(self) -> None:
        """Send the full initial dashboard state on connect."""
        try:
            snapshot  = await self._get_snapshot()
            trades    = await self._get_recent_trades()
            bias_data = await self._get_bias()

            await self.send(text_data=json.dumps({
                "event":    "initial_state",
                "snapshot": snapshot,
                "trades":   trades,
                "bias":     bias_data,
            }))
        except Exception as exc:
            logger.error("DashboardConsumer: initial state failed — %s", exc)
            await self.send(text_data=json.dumps({
                "event": "initial_state",
                "error": str(exc),
            }))

    async def _account_tick_loop(self) -> None:
        """
        Background coroutine: fetches account snapshot every ACCOUNT_TICK_INTERVAL
        seconds and pushes to the channel group (so all connected clients update).
        Runs until the WebSocket disconnects.
        """
        while True:
            await asyncio.sleep(ACCOUNT_TICK_INTERVAL)
            try:
                snapshot = await self._get_snapshot()
                await self.channel_layer.group_send(DASHBOARD_GROUP, {
                    "type":    "account.tick",
                    "payload": snapshot,
                })
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("DashboardConsumer: tick loop error — %s", exc)

    @database_sync_to_async
    def _get_snapshot(self) -> dict:
        """Fetch account snapshot from MT5 via the trade executor."""
        try:
            from forex.services.core.trade_executor import TradeExecutor
            snap = TradeExecutor().get_account_snapshot()
            return {
                "balance":        snap.balance,
                "equity":         snap.equity,
                "free_margin":    snap.free_margin,
                "margin_level":   snap.margin_level,
                "floating_pnl":   snap.total_floating_pnl,
                "open_trades":    snap.open_trade_count,
                "drawdown_pct":   snap.drawdown_pct,
                "positions": [
                    {
                        "ticket":        p.ticket,
                        "symbol":        p.symbol,
                        "action":        p.action,
                        "lot_size":      p.lot_size,
                        "open_price":    p.open_price,
                        "current_price": p.current_price,
                        "profit":        p.profit,
                        "comment":       p.comment,
                    }
                    for p in snap.positions
                ],
            }
        except Exception as exc:
            logger.warning("DashboardConsumer._get_snapshot: %s", exc)
            return {"error": str(exc)}

    @database_sync_to_async
    def _get_recent_trades(self) -> list[dict]:
        """Get the 50 most recent TradeLog entries from the database."""
        try:
            from forex.models import TradeLog
            logs = TradeLog.objects.order_by("-received_at")[:50]
            return [
                {
                    "id":            t.pk,
                    "symbol":        t.symbol,
                    "action":        t.action,
                    "status":        t.status,
                    "lot_size":      t.lot_size,
                    "entry_price":   t.entry_price,
                    "exit_price":    t.exit_price,
                    "profit":        t.profit,
                    "llm_approved":  t.llm_approved,
                    "llm_reasoning": t.llm_reasoning,
                    "llm_model":     t.llm_model_used,
                    "signal_source": t.signal_source,
                    "received_at":   t.received_at.isoformat(),
                }
                for t in logs
            ]
        except Exception as exc:
            logger.warning("DashboardConsumer._get_recent_trades: %s", exc)
            return []

    @database_sync_to_async
    def _get_bias(self) -> dict:
        """Get current session bias from Redis."""
        try:
            from forex.services.market.bias_cache import SessionBiasCache
            record = SessionBiasCache().get("EURUSD")
            if record is None:
                return {"bias": "NEUTRAL", "confidence": 0, "reasoning": "No analysis yet"}
            return {
                "bias":       record.bias.value,
                "confidence": record.confidence,
                "reasoning":  record.reasoning,
                "model_used": record.model_used,
                "set_at":     record.set_at.isoformat(),
            }
        except Exception as exc:
            return {"bias": "NEUTRAL", "confidence": 0, "reasoning": str(exc)}