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
    queue="trades",
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
    from forex.services.core.market_hours import is_market_open
    from forex.services.market.ohlcv import OHLCVFetcher
    from forex.services.llm.chart_analyzer import ChartAnalyzer
    from forex.services.market.bias_cache import SessionBiasCache
    from forex.services.notifications.telegram import TelegramNotifier

    # Skip if market is closed
    if not is_market_open(symbol):
        logger.info(
            "run_strategic_analysis: %s market closed — skipping", symbol
        )
        return {"outcome": "skipped", "reason": "market_closed", "symbol": symbol}


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

    if outcome.get("outcome") == "executed" and outcome.get("ticket") is not None:
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


@shared_task(name="forex.run_position_watcher")
def run_position_watcher(symbol: str = "EURUSD") -> dict:
    """
    Lightweight position watcher that can be scheduled more frequently than
    the HFT scanner. It fetches the current account snapshot and asks the
    HFTScanner to re-evaluate early-exit opportunities using short-window
    microtrend checks. This task is intentionally small and safe to run
    often (e.g., every 5 seconds) if your Celery scheduler allows it.
    """
    try:
        import redis as redis_lib
        from forex.services.market.hft_scanner import HFTScanner, SCALP_COMMENT
        from forex.services.notifications.telegram import TelegramNotifier
        from forex.services.config.settings import forex_settings

        # Redis client for storing per-ticket peak PnL
        url = getattr(forex_settings, 'redis_url', 'redis://localhost:6379/0')
        r = redis_lib.from_url(url, decode_responses=True)

        # Use a fresh executor to fetch the full account snapshot so we
        # consider all open positions regardless of symbol or comment.
        from forex.services.core.trade_executor import TradeExecutor
        exec = TradeExecutor()
        try:
            snapshot = exec.get_account_snapshot()
        except Exception as exc:
            logger.debug("run_position_watcher: snapshot failed — %s", exc)
            return {"outcome": "snapshot_failed", "error": str(exc)}

        # Consider all open positions
        all_positions = list(snapshot.positions)
        if not all_positions:
            return {"outcome": "no_positions"}

        exits = []
        dry_log = []

        for pos in all_positions:
            try:
                ticket_key = f"lykhan:watcher:peak:{pos.ticket}"
                # read previous peak
                raw = r.get(ticket_key)
                prev_peak = float(raw) if raw is not None else None

                cur_profit = float(pos.profit)
                # update peak if current profit is higher
                if prev_peak is None or cur_profit > prev_peak:
                    r.setex(ticket_key, forex_settings.early_exit_peak_ttl_seconds, str(cur_profit))
                    prev_peak = cur_profit

                # FORCE-CLOSE: if absolute profit meets configured threshold, close immediately
                if cur_profit >= forex_settings.early_exit_force_profit_amount:
                    # Attempt to close via executor; on REJECTED, apply robust handling
                    try:
                        result = exec.close_trade(pos.ticket)
                    except Exception as exc:
                        logger.warning('run_position_watcher: close_trade raised for %s — %s', pos.ticket, exc)
                        dry_log.append({'ticket': pos.ticket, 'reason': 'close_exception', 'error': str(exc)})
                        result = None

                    # If result indicates success, record exit
                    if result is not None and getattr(result, 'status', None) in ('EXECUTED', 'CLOSED'):
                        exits.append({'ticket': pos.ticket, 'profit': cur_profit, 'status': result.status, 'reason': 'force_profit'})
                        r.delete(ticket_key)
                        continue

                    # If rejected or error, inspect and try robust recovery
                    err_msg = getattr(result, 'error_message', '') if result is not None else ''
                    if result is not None and result.status == 'REJECTED' and 'Ticket not found' in (err_msg or ''):
                        # Refresh snapshot to confirm whether ticket still exists
                        try:
                            fresh = exec.get_account_snapshot()
                            exists = any(p.ticket == pos.ticket for p in getattr(fresh, 'positions', []))
                        except Exception:
                            exists = True

                        if not exists:
                            # Already closed externally — clear peak tracking
                            r.delete(ticket_key)
                            dry_log.append({'ticket': pos.ticket, 'reason': 'already_closed'})
                            continue
                        # If still exists, fall through to retry attempts below

                    # Retry with explicit TradeCommand including symbol and progressively larger slippage
                    from forex.services.core.schemas import TradeCommand, TradeAction
                    base_slippage = getattr(exec, '_slippage', forex_settings.default_slippage)
                    retries = 0
                    closed = False
                    while retries < forex_settings.force_close_max_retries and not closed:
                        retries += 1
                        try_slip = base_slippage + retries * forex_settings.force_close_slippage_increment
                        cmd = TradeCommand(
                            action = TradeAction.CLOSE,
                            ticket = pos.ticket,
                            symbol = pos.symbol,
                            lot_size = getattr(pos, 'lot_size', exec._lot),
                            slippage = try_slip,
                            magic = getattr(pos, 'magic', exec._magic),
                        )
                        try:
                            res = exec._execute(cmd)
                        except Exception as exc:
                            logger.warning('run_position_watcher: retry close exec._execute failed for %s — %s', pos.ticket, exc)
                            dry_log.append({'ticket': pos.ticket, 'reason': 'retry_exception', 'error': str(exc)})
                            res = None

                        if res is not None and getattr(res, 'status', None) in ('EXECUTED', 'CLOSED'):
                            exits.append({'ticket': pos.ticket, 'profit': cur_profit, 'status': res.status, 'reason': 'force_profit_retry', 'attempts': retries})
                            r.delete(ticket_key)
                            closed = True
                            break
                        else:
                            # If rejected for ticket not found after retry, refresh and bail
                            if res is not None and getattr(res, 'status', None) == 'REJECTED' and res.error_message and 'Ticket not found' in res.error_message:
                                try:
                                    fresh = exec.get_account_snapshot()
                                    exists = any(p.ticket == pos.ticket for p in getattr(fresh, 'positions', []))
                                except Exception:
                                    exists = True
                                if not exists:
                                    r.delete(ticket_key)
                                    dry_log.append({'ticket': pos.ticket, 'reason': 'already_closed_after_retry'})
                                    closed = True
                                    break
                            # otherwise wait a short moment before next retry
                            import time as _time
                            _time.sleep(0.2)

                    if not closed:
                        dry_log.append({'ticket': pos.ticket, 'reason': 'force_close_failed_final', 'attempts': retries})

                # compute pullback from peak
                trigger_pullback = False
                if prev_peak and prev_peak > 0:
                    drop = (prev_peak - cur_profit) / prev_peak
                    if drop >= forex_settings.early_exit_pullback_pct and cur_profit >= forex_settings.early_exit_min_profit_amount:
                        trigger_pullback = True

                # call scanner logic to decide early exit (this may close)
                # use a dry_run flag to capture decision without closing when needed
                # For now, perform real action if pullback triggered; otherwise use scanner's existing rules
                if trigger_pullback:
                    # re-evaluate short-window microtrend for the position's symbol
                    fetcher = scanner._fetcher
                    micro = None
                    try:
                        micro = fetcher.fetch_candles(pos.symbol, "M1", count=12)
                    except Exception:
                        micro = None
                    ind = compute_indicators(micro) if micro else {}
                    ema9 = ind.get('ema9'); ema21 = ind.get('ema21'); macd_dir = ind.get('macd_direction')
                    hold_due_to_momentum = False
                    if pos.action == 'BUY' and ema9 and ema21 and ema9 > ema21 and macd_dir and macd_dir.startswith('bullish'):
                        hold_due_to_momentum = True
                    if pos.action == 'SELL' and ema9 and ema21 and ema9 < ema21 and macd_dir and macd_dir.startswith('bearish'):
                        hold_due_to_momentum = True

                    if hold_due_to_momentum:
                        dry_log.append({'ticket': pos.ticket, 'reason': 'momentum_hold', 'peak': prev_peak, 'profit': cur_profit})
                    else:
                        # close
                        try:
                            result = scanner._exec.close_trade(pos.ticket)
                            exits.append({'ticket': pos.ticket, 'profit': cur_profit, 'status': result.status})
                            r.delete(ticket_key)
                        except Exception as exc:
                            logger.warning('run_position_watcher: failed to close %s — %s', pos.ticket, exc)
                            dry_log.append({'ticket': pos.ticket, 'reason': 'close_failed', 'error': str(exc)})
                else:
                    # No pullback; optionally run scanner's _check_early_exits to catch other rules
                    # We'll call it in a dry manner by invoking internal method but not closing here.
                    dry_log.append({'ticket': pos.ticket, 'reason': 'no_pullback', 'peak': prev_peak, 'profit': cur_profit})

            except Exception as exc:
                logger.exception('run_position_watcher: per-position error')
                dry_log.append({'ticket': getattr(pos, 'ticket', None), 'error': str(exc)})

        if exits:
            _broadcast_trade_event({'outcome': 'early_exits', 'exits': exits})

        return {'outcome': 'checked', 'exits': exits, 'dry_log': dry_log}

    except Exception as exc:
        logger.exception("run_position_watcher: unexpected error")
        return {"outcome": "error", "error": str(exc)}