"""
forex/tasks.py
───────────────
All Celery tasks for the Lykhan Forex sub-agent.

Task inventory
───────────────
process_trade_signal    — async pipeline: LLM gate → MT5 execute (triggered by webhook)
run_strategic_analysis  — every 30min: OHLCV fetch → LLM → session bias → Redis
run_hft_scan            — every 30s: M1 tick check → entry signal → scalp trade
send_telegram_summary   — every 30min: account snapshot → Telegram digest
check_bridge_health     — every 5min: heartbeat check → alert if bridge offline

Celery Beat schedule (add to CELERY_BEAT_SCHEDULE in settings.py):

    from celery.schedules import crontab
    CELERY_BEAT_SCHEDULE = {
        "strategic-analysis": {
            "task":     "forex.run_strategic_analysis",
            "schedule": 1800,           # every 30 minutes
            "args":     ("EURUSD",),
        },
        "hft-scan": {
            "task":     "forex.run_hft_scan",
            "schedule": 30,             # every 30 seconds
            "args":     ("EURUSD",),
        },
        "telegram-summary": {
            "task":     "forex.send_telegram_summary",
            "schedule": 1800,           # every 30 minutes
        },
        "bridge-health": {
            "task":     "forex.check_bridge_health",
            "schedule": 300,            # every 5 minutes
        },
    }

SQLite / concurrency note:
Run with --concurrency=1 while on SQLite:
    celery -A project worker --loglevel=info --concurrency=1
    celery -A project beat  --loglevel=info
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone as dj_timezone

logger = logging.getLogger(__name__)


# ── Task 1: TradingView signal pipeline ───────────────────────────────────────

@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=15,
    name="forex.process_trade_signal",
)
def process_trade_signal(self, trade_log_id: int) -> dict:
    """
    Full async pipeline for an inbound TradingView webhook signal:
    Account snapshot → LLM hard gate → MT5 execution → TradeLog + Telegram.

    :param trade_log_id: PK of the TradeLog row created by SignalWebhookView.
    """
    from forex.models import TradeLog, TradeStatus
    from forex.services.core.trade_executor import TradeExecutor, TradeValidationError
    from forex.services.llm.gate import LLMGate, build_gate_context
    from forex.services.notifications.telegram import TelegramNotifier

    try:
        log = TradeLog.objects.get(pk=trade_log_id)
    except TradeLog.DoesNotExist:
        logger.error("process_trade_signal: TradeLog %d not found", trade_log_id)
        return {"error": f"TradeLog {trade_log_id} not found"}

    logger.info(
        "process_trade_signal: %s %s id=%d source=%s",
        log.action, log.symbol, log.pk, log.signal_source,
    )

    executor  = TradeExecutor()
    notifier  = TelegramNotifier()

    # Account snapshot
    try:
        snapshot = executor.get_account_snapshot()
        log.balance_at_entry      = snapshot.balance
        log.equity_at_entry       = snapshot.equity
        log.free_margin_at_entry  = snapshot.free_margin
        log.open_trades_at_entry  = snapshot.open_trade_count
        log.save(update_fields=[
            "balance_at_entry", "equity_at_entry",
            "free_margin_at_entry", "open_trades_at_entry",
        ])
    except Exception as exc:
        logger.warning("process_trade_signal: snapshot failed — %s", exc)
        snapshot = None

    balance     = getattr(snapshot, "balance",          0.0)
    equity      = getattr(snapshot, "equity",           0.0)
    free_margin = getattr(snapshot, "free_margin",      0.0)
    open_count  = getattr(snapshot, "open_trade_count",  0)

    # LLM hard gate
    context  = build_gate_context(
        symbol=log.symbol, action=log.action, lot_size=log.lot_size,
        sl_pips=log.sl_pips, tp_pips=log.tp_pips,
        balance=balance, equity=equity, free_margin=free_margin,
        open_trades_count=open_count,
        indicators=log.signal_indicators or {},
        signal_source=log.signal_source, comment=log.comment,
    )
    decision = LLMGate().evaluate(context)

    log.llm_approved   = decision.approved
    log.llm_reasoning  = decision.reasoning
    log.llm_model_used = decision.model_used
    log.llm_latency_ms = decision.latency_ms
    log.llm_confidence = decision.confidence
    log.save(update_fields=[
        "llm_approved", "llm_reasoning", "llm_model_used",
        "llm_latency_ms", "llm_confidence",
    ])

    # Notify Telegram about gate decision
    notifier.send_llm_gate_decision(
        symbol=log.symbol, action=log.action,
        approved=decision.approved, reasoning=decision.reasoning,
        model_used=decision.model_used, confidence=decision.confidence,
    )

    if not decision.approved:
        log.status = TradeStatus.REJECTED
        log.save(update_fields=["status"])
        return {
            "outcome":    "rejected",
            "model_used": decision.model_used,
            "reasoning":  decision.reasoning,
        }

    # Execute
    import time
    log.status = TradeStatus.APPROVED
    log.save(update_fields=["status"])

    exec_start = time.monotonic()
    try:
        if log.action == "BUY":
            result = executor.open_buy(
                symbol=log.symbol, lot_size=log.lot_size,
                sl_pips=log.sl_pips, tp_pips=log.tp_pips,
                comment=log.comment or "lykhan-tv-buy",
            )
        elif log.action == "SELL":
            result = executor.open_sell(
                symbol=log.symbol, lot_size=log.lot_size,
                sl_pips=log.sl_pips, tp_pips=log.tp_pips,
                comment=log.comment or "lykhan-tv-sell",
            )
        else:
            raise TradeValidationError(f"Unsupported action: {log.action!r}")

        exec_ms = int((time.monotonic() - exec_start) * 1000)

        if result.status in ("EXECUTED", "CLOSED"):
            log.ticket               = result.ticket
            log.entry_price          = result.open_price
            log.status               = TradeStatus.EXECUTED
            log.executed_at          = dj_timezone.now()
            log.execution_latency_ms = exec_ms
            log.save(update_fields=[
                "ticket", "entry_price", "status",
                "executed_at", "execution_latency_ms",
            ])
            notifier.send_trade_opened(
                symbol=log.symbol, action=log.action,
                ticket=result.ticket, price=result.open_price,
                sl_pips=log.sl_pips, tp_pips=log.tp_pips,
                source="signal",
            )
            return {"outcome": "executed", "ticket": result.ticket,
                    "entry_price": result.open_price, "latency_ms": exec_ms}
        else:
            log.status        = TradeStatus.ERROR
            log.error_message = result.error_message
            log.save(update_fields=["status", "error_message"])
            return {"outcome": "mt5_error", "error": result.error_message}

    except TradeValidationError as exc:
        log.status = TradeStatus.ERROR
        log.error_message = str(exc)
        log.save(update_fields=["status", "error_message"])
        return {"outcome": "validation_error", "error": str(exc)}
    except Exception as exc:
        log.status = TradeStatus.ERROR
        log.error_message = str(exc)
        log.save(update_fields=["status", "error_message"])
        raise self.retry(exc=exc)


# ── Task 2: Strategic LLM analysis (every 30 min) ────────────────────────────

@shared_task(name="forex.run_strategic_analysis")
def run_strategic_analysis(symbol: str = "EURUSD") -> dict:
    """
    Fetches multi-timeframe OHLCV data, sends to LLM for market analysis,
    stores session bias in Redis. Runs every 30 minutes via Celery Beat.
    """
    from forex.services.market.ohlcv import OHLCVFetcher
    from forex.services.llm.chart_analyzer import ChartAnalyzer
    from forex.services.market.bias_cache import SessionBiasCache
    from forex.services.notifications.telegram import TelegramNotifier

    logger.info("run_strategic_analysis: starting for %s", symbol)

    fetcher  = OHLCVFetcher()
    analyzer = ChartAnalyzer()
    cache    = SessionBiasCache()
    notifier = TelegramNotifier()

    candle_data = fetcher.fetch_multi_timeframe(symbol)

    if not candle_data:
        logger.error("run_strategic_analysis: no candle data fetched — is MT5 bridge alive?")
        notifier.send_critical_alert(
            "OHLCV fetch failed",
            f"Could not fetch any candle data for {symbol}. Is the MT5 bridge running?",
        )
        return {"outcome": "failed", "reason": "no_candle_data"}

    result = analyzer.analyze(symbol, candle_data)

    cache.set(
        symbol     = symbol,
        bias       = result.bias,
        confidence = result.confidence,
        reasoning  = result.reasoning,
        model_used = result.model_used,
    )

    notifier.send_session_bias_update(
        symbol     = symbol,
        bias       = result.bias.value,
        confidence = result.confidence,
        reasoning  = result.reasoning,
        model_used = result.model_used,
    )

    # Push to dashboard WebSocket
    _broadcast_bias_update(symbol, result)

    logger.info(
        "run_strategic_analysis: %s bias=%s confidence=%d model=%s",
        symbol, result.bias.value, result.confidence, result.model_used,
    )
    return {
        "outcome":    "success",
        "symbol":     symbol,
        "bias":       result.bias.value,
        "confidence": result.confidence,
        "model_used": result.model_used,
    }


# ── Task 3: HFT scan (every 30 seconds) ──────────────────────────────────────

@shared_task(name="forex.run_hft_scan")
def run_hft_scan(symbol: str = "EURUSD") -> dict:
    """
    M1 scalping scanner. Reads session bias from Redis, checks M1 indicators,
    and fires scalp trades when signal aligns with bias. Runs every 30 seconds.
    """
    from forex.services.market.hft_scanner import HFTScanner
    from forex.services.notifications.telegram import TelegramNotifier
    from forex.models import TradeLog, TradeStatus, SignalSource
    import uuid

    scanner  = HFTScanner(symbol=symbol)
    notifier = TelegramNotifier()
    outcome  = scanner.scan()

    if outcome.get("outcome") == "executed":
        # Persist to TradeLog
        try:
            log = TradeLog.objects.create(
                command_id        = str(uuid.uuid4()),
                symbol            = symbol,
                action            = outcome["signal"],
                lot_size          = 0.01,
                sl_pips           = outcome.get("sl_pips", 30),
                tp_pips           = outcome.get("tp_pips", 60),
                comment           = "lykhan-hft",
                signal_source     = SignalSource.LLM,
                signal_payload    = outcome,
                signal_indicators = outcome.get("indicators", {}),
                status            = TradeStatus.EXECUTED,
                ticket            = outcome.get("ticket"),
                entry_price       = outcome.get("entry_price"),
                executed_at       = dj_timezone.now(),
            )
        except Exception as exc:
            logger.error("run_hft_scan: failed to write TradeLog — %s", exc)

        notifier.send_trade_opened(
            symbol   = symbol,
            action   = outcome["signal"],
            ticket   = outcome.get("ticket"),
            price    = outcome.get("entry_price"),
            sl_pips  = outcome.get("sl_pips", 30),
            tp_pips  = outcome.get("tp_pips", 60),
            source   = "hft",
        )

        _broadcast_trade_event(outcome)

    return outcome


# ── Task 4: Telegram 30-min summary ──────────────────────────────────────────

@shared_task(name="forex.send_telegram_summary")
def send_telegram_summary() -> dict:
    """
    Fetches account snapshot + trade stats for the last 30 minutes
    and sends a digest to Telegram. Runs every 30 minutes via Celery Beat.
    """
    from forex.services.core.trade_executor import TradeExecutor
    from forex.services.market.bias_cache import SessionBiasCache
    from forex.services.notifications.telegram import TelegramNotifier
    from forex.models import TradeLog, TradeStatus
    from django.utils import timezone as dj_tz
    from datetime import timedelta

    notifier = TelegramNotifier()
    executor = TradeExecutor()
    cache    = SessionBiasCache()

    try:
        snapshot = executor.get_account_snapshot()
    except Exception as exc:
        logger.error("send_telegram_summary: snapshot failed — %s", exc)
        return {"outcome": "failed", "error": str(exc)}

    # Trades in the last 30 minutes
    since = dj_tz.now() - timedelta(minutes=30)
    recent = TradeLog.objects.filter(
        received_at__gte=since,
        status__in=[TradeStatus.EXECUTED, TradeStatus.CLOSED],
    )
    net_pnl = sum(t.profit or 0 for t in recent if t.profit is not None)

    bias_record = cache.get("EURUSD")
    bias        = bias_record.bias.value if bias_record else "NEUTRAL"
    confidence  = bias_record.confidence if bias_record else 0

    notifier.send_30min_summary(
        balance        = snapshot.balance,
        equity         = snapshot.equity,
        open_trades    = snapshot.open_trade_count,
        trades_count   = recent.count(),
        net_pnl        = net_pnl,
        session_bias   = bias,
        bias_confidence = confidence,
    )
    return {"outcome": "sent", "trades": recent.count(), "net_pnl": net_pnl}


# ── Task 5: Bridge health check (every 5 min) ─────────────────────────────────

@shared_task(name="forex.check_bridge_health")
def check_bridge_health() -> dict:
    """
    Checks the MT5 FileBridge heartbeat. Sends a critical Telegram alert
    if the bridge has been offline for more than one check interval.
    """
    from forex.services.core.trade_executor import TradeExecutor
    from forex.services.notifications.telegram import TelegramNotifier

    alive    = TradeExecutor().check_bridge()
    notifier = TelegramNotifier()

    if not alive:
        notifier.send_critical_alert(
            "MT5 Bridge Offline",
            "The LykhanBridge EA heartbeat has stopped. "
            "Check that MT5 is running in Bottles and the EA is attached to a chart.",
        )
    return {"bridge_alive": alive}


# ── Dashboard broadcast helpers ───────────────────────────────────────────────

def _broadcast_bias_update(symbol: str, result) -> None:
    """Push session bias update to all connected WebSocket dashboard clients."""
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        layer = get_channel_layer()
        async_to_sync(layer.group_send)("lykhan_dashboard", {
            "type":       "bias.update",
            "symbol":     symbol,
            "bias":       result.bias.value,
            "confidence": result.confidence,
            "reasoning":  result.reasoning,
            "model_used": result.model_used,
        })
    except Exception as exc:
        logger.debug("_broadcast_bias_update: channel layer not available — %s", exc)


def _broadcast_trade_event(outcome: dict) -> None:
    """Push a trade event to all connected WebSocket dashboard clients."""
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        layer = get_channel_layer()
        async_to_sync(layer.group_send)("lykhan_dashboard", {
            "type":    "trade.event",
            "payload": outcome,
        })
    except Exception as exc:
        logger.debug("_broadcast_trade_event: channel layer not available — %s", exc)