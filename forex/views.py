"""
forex/views.py
───────────────
TradingView webhook endpoint for the Lykhan Forex sub-agent.

This view has one job: receive the signal, validate it, create a PENDING
TradeLog row, and drop a Celery task onto the queue. It must return a
200 response within ~2 seconds or TradingView will mark the webhook as
failed and eventually stop sending alerts.

The heavy work (LLM gate + MT5 execution) runs asynchronously in Celery.

TradingView alert JSON format
───────────────────────────────
Configure your TradingView alert's "Message" field to send this JSON:

{
  "secret": "{{your WEBHOOK_SECRET from .env}}",
  "action": "BUY",
  "symbol": "EURUSD",
  "lot_size": 0.01,
  "sl_pips": 50,
  "tp_pips": 100,
  "comment": "EMA 9/21 crossover H1",
  "indicators": {
    "rsi": 38.2,
    "macd": "bullish",
    "ema_cross": "golden",
    "timeframe": "H1",
    "atr": 14.5
  }
}

Set the alert's webhook URL to:
    https://your-domain.com/forex/webhook/signal/

Security: every request must include a "secret" field matching
WEBHOOK_SECRET in your .env file. Requests without it are rejected 401.
"""
from __future__ import annotations

import json
import logging
import os
import uuid

from django.http import JsonResponse
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

logger = logging.getLogger(__name__)

# Supported trade actions the webhook accepts.
# CLOSE and CLOSE_ALL via webhook are intentionally excluded — those should
# only be triggered manually or by a dedicated risk management task.
ALLOWED_ACTIONS = {"BUY", "SELL"}


@method_decorator(csrf_exempt, name="dispatch")
class SignalWebhookView(View):
    """
    POST /forex/webhook/signal/

    Receives a TradingView alert, validates it, creates a TradeLog row,
    and dispatches process_trade_signal to the Celery queue.

    Returns:
        200 {"status": "queued", "trade_log_id": N}  — signal accepted
        400 {"error": "..."}                          — invalid payload
        401 {"error": "invalid secret"}               — wrong webhook secret
        405 {"error": "method not allowed"}           — non-POST request
        500 {"error": "..."}                          — unexpected server error
    """

    def post(self, request):
        # ── Parse body ────────────────────────────────────────────────────────
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Webhook: malformed JSON — %s", exc)
            return JsonResponse({"error": "request body must be valid JSON"}, status=400)

        # ── Authenticate ──────────────────────────────────────────────────────
        expected_secret = os.getenv("WEBHOOK_SECRET", "")
        if not expected_secret:
            logger.error("Webhook: WEBHOOK_SECRET not set in .env — all requests rejected")
            return JsonResponse({"error": "server misconfiguration"}, status=500)

        if payload.get("secret") != expected_secret:
            logger.warning("Webhook: invalid secret from %s", request.META.get("REMOTE_ADDR"))
            return JsonResponse({"error": "invalid secret"}, status=401)

        # ── Validate required fields ──────────────────────────────────────────
        action = str(payload.get("action", "")).upper()
        symbol = str(payload.get("symbol", "EURUSD")).upper()

        if action not in ALLOWED_ACTIONS:
            return JsonResponse(
                {"error": f"action must be one of {sorted(ALLOWED_ACTIONS)}, got {action!r}"},
                status=400,
            )

        try:
            lot_size = float(payload.get("lot_size", 0.01))
            sl_pips  = int(payload.get("sl_pips", 0))
            tp_pips  = int(payload.get("tp_pips", 0))
        except (TypeError, ValueError) as exc:
            return JsonResponse({"error": f"invalid numeric field: {exc}"}, status=400)

        if lot_size < 0.01 or lot_size > 100.0:
            return JsonResponse({"error": "lot_size must be between 0.01 and 100.0"}, status=400)

        # ── Create pending TradeLog row ────────────────────────────────────────
        # Import here (not at module top) to avoid import issues if Django
        # isn't fully initialised when the module is first loaded.
        from forex.models import TradeLog, TradeStatus, SignalSource
        from forex.tasks import process_trade_signal

        try:
            log = TradeLog.objects.create(
                command_id        = str(uuid.uuid4()),
                symbol            = symbol,
                action            = action,
                lot_size          = lot_size,
                sl_pips           = sl_pips,
                tp_pips           = tp_pips,
                comment           = str(payload.get("comment", "lykhan-webhook"))[:100],
                signal_source     = SignalSource.TRADINGVIEW,
                signal_payload    = payload,
                signal_indicators = payload.get("indicators") or {},
                status            = TradeStatus.PENDING,
            )
        except Exception as exc:
            logger.exception("Webhook: failed to create TradeLog row")
            return JsonResponse({"error": "database write failed"}, status=500)

        # ── Dispatch async Celery task ─────────────────────────────────────────
        try:
            process_trade_signal.apply_async(
                args=[log.pk],
                queue="trades",
                expires=60,    # discard if not picked up within 60 seconds
            )
        except Exception as exc:
            # If Celery/Redis is down, mark the row as error rather than leaving
            # it in PENDING state forever.
            log.status        = TradeStatus.ERROR
            log.error_message = f"Failed to queue Celery task: {exc}"
            log.save(update_fields=["status", "error_message"])
            logger.error("Webhook: Celery dispatch failed — %s", exc)
            return JsonResponse({"error": "task queue unavailable"}, status=500)

        logger.info(
            "Webhook: queued %s %s (lot=%.2f) — TradeLog id=%d",
            action, symbol, lot_size, log.pk,
        )
        return JsonResponse({"status": "queued", "trade_log_id": log.pk}, status=200)



def dashboard_view(request):
    from django.shortcuts import render
    return render(request, "dashboard/index.html")
    
