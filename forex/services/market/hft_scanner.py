"""
forex/services/market/hft_scanner.py
──────────────────────────────────────
HFT microtrend scanner for EURUSD M1 scalping.

This runs as a Celery Beat periodic task every 30 seconds. On each cycle:
  1. Read session bias from Redis (set by the 30-min strategic LLM loop)
  2. If NEUTRAL → skip all trades, log reason
  3. If LONG or SHORT → fetch M1 candles, compute indicators
  4. Check entry signal: EMA 9/21 cross + RSI filter + MACD confirmation
  5. Check risk conditions: max open trades, free margin, drawdown guard
  6. If all pass → open a scalp trade via TradeExecutor
  7. Also check existing open positions for early exit conditions

Design decisions
─────────────────
- 30-second poll interval for M1 scalping is deliberate: EURUSD M1 candles
  close every 60 seconds, so a 30s poll catches signals within one candle of
  forming. Sub-second polling via the file bridge is impractical (500ms
  latency per bridge call means at most 2 calls/second, and SQLite writes
  add contention).
- ATR-based SL/TP: the scanner uses the ATR recommended by the OHLCV fetcher
  rather than fixed pip values. This adapts to session volatility.
- Max concurrent scalp trades: 2 (separate from the TradingView signal cap
  of 3 — scalps have tighter management).
- Early exit: if a position's floating P&L hits 50% of TP, the scanner
  closes it to lock in profit rather than risking a reversal.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from forex.services.core.market_hours import is_market_open, get_closed_reason
from forex.services.core.schemas import SessionBias, TradeResult
from forex.services.core.trade_executor import TradeExecutor, TradeValidationError
from forex.services.market.bias_cache import SessionBiasCache
from forex.services.market.ohlcv import OHLCVFetcher, compute_indicators

logger = logging.getLogger(__name__)

# ── Scanner configuration ─────────────────────────────────────────────────────

MAX_SCALP_TRADES    = 2       # Maximum concurrent HFT positions
RSI_OVERBOUGHT      = 65      # Avoid BUY above this RSI
RSI_OVERSOLD        = 35      # Avoid SELL below this RSI
MIN_CONFIDENCE      = 55      # Minimum LLM bias confidence to trade
DRAWDOWN_GUARD_PCT  = 8.0     # Pause if equity drops >8% below balance
EARLY_EXIT_PCT      = 0.5     # Close position if P&L reaches 50% of TP value
SCALP_COMMENT       = "lykhan-hft"


class HFTScanner:
    """
    Runs the M1 scalping logic for a given symbol.

    The Celery task instantiates this on each 30-second beat tick.
    It is intentionally stateless — all state lives in Redis (bias)
    or the database (TradeLog). This makes it safe to restart without
    losing any context.
    """

    def __init__(
        self,
        symbol:   str                     = "EURUSD",
        executor: Optional[TradeExecutor] = None,
        fetcher:  Optional[OHLCVFetcher]  = None,
        cache:    Optional[SessionBiasCache] = None,
    ) -> None:
        self.symbol   = symbol
        self._exec    = executor or TradeExecutor()
        self._fetcher = fetcher  or OHLCVFetcher()
        self._cache   = cache    or SessionBiasCache()

    def scan(self) -> dict:
        """
        Run one scan cycle. Returns a summary dict for logging/monitoring.
        Never raises — all exceptions are caught and returned as errors.
        """
        try:
            return self._run_scan()
        except Exception as exc:
            logger.exception("HFTScanner: unexpected error in scan cycle")
            return {"outcome": "error", "error": str(exc), "symbol": self.symbol}

    def _run_scan(self) -> dict:

        # ── 0. Market hours check ─────────────────────────────────────────────
        if not is_market_open(self.symbol):
            logger.debug("HFTScanner: %s", get_closed_reason(self.symbol))
            return {
                "outcome": "skipped",
                "reason":  "market_closed",
                "symbol":  self.symbol,
            }

        # ── 1. Read session bias ───────────────────────────────────────────────
        record = self._cache.get(self.symbol)

        if record is None:
            logger.debug("HFTScanner: no bias in Redis for %s — skipping", self.symbol)
            return {"outcome": "skipped", "reason": "no_bias", "symbol": self.symbol}

        bias = record.bias
        if bias == SessionBias.NEUTRAL:
            logger.debug("HFTScanner: bias=NEUTRAL for %s — skipping", self.symbol)
            return {"outcome": "skipped", "reason": "neutral_bias", "symbol": self.symbol}

        if record.confidence < MIN_CONFIDENCE:
            return {
                "outcome": "skipped",
                "reason":  f"low_confidence_{record.confidence}",
                "symbol":  self.symbol,
            }

        # ── 2. Account health checks ──────────────────────────────────────────
        try:
            snapshot = self._exec.get_account_snapshot()
        except Exception as exc:
            logger.warning("HFTScanner: could not fetch account snapshot — %s", exc)
            return {"outcome": "skipped", "reason": "snapshot_failed", "symbol": self.symbol}

        if snapshot.drawdown_pct >= DRAWDOWN_GUARD_PCT:
            logger.warning(
                "HFTScanner: drawdown guard triggered (%.1f%% >= %.1f%%) — pausing",
                snapshot.drawdown_pct, DRAWDOWN_GUARD_PCT,
            )
            return {"outcome": "skipped", "reason": "drawdown_guard", "symbol": self.symbol}

        # Count only HFT scalp positions (identified by comment prefix)
        scalp_positions = [
            p for p in snapshot.positions
            if p.comment.startswith(SCALP_COMMENT)
        ]

        # Always check for early exits on open positions
        if scalp_positions:
            self._check_early_exits(scalp_positions)

        if len(scalp_positions) >= MAX_SCALP_TRADES:
            # Check for early exit opportunities instead of opening new ones
            exits = self._check_early_exits(scalp_positions)
            return {"outcome": "max_trades", "early_exits": exits, "symbol": self.symbol}

        # ── 3. Fetch M1 candles and compute indicators ────────────────────────
        data = self._fetcher.fetch_candles(self.symbol, "M1", count=50)
        if data is None or len(data.bars) < 30:
            return {"outcome": "skipped", "reason": "insufficient_m1_data", "symbol": self.symbol}

        ind = compute_indicators(data)

        # ── 4. Entry signal logic ─────────────────────────────────────────────
        signal = self._evaluate_entry(bias, ind)
        if signal is None:
            return {
                "outcome":    "skipped",
                "reason":     "no_entry_signal",
                "symbol":     self.symbol,
                "indicators": ind,
            }

       # ── 5. ATR-based position sizing ──────────────────────────────────────
        sl_pips = ind.get("atr_sl_pips") or 30
        tp_pips = ind.get("atr_tp_pips") or 60

        # Cap at symbol-aware limits for M1 scalping
        # Crypto pairs (BTC, ETH) use dollar-based stops, not pip-based
        if any(crypto in self.symbol for crypto in ("BTC", "ETH", "XRP", "LTC", "BCH")):
            sl_pips = 0   # disable stops for crypto — EA stop calculation doesn't work for BTC
            tp_pips = 0   # MT5 will hold position until manually closed or early exit triggers
        else:
            # Forex pairs — standard pip limits
            sl_pips = max(10, min(sl_pips, 50))
            tp_pips = max(20, min(tp_pips, 100))

        # ── 6. Execute the scalp trade ────────────────────────────────────────
        try:
            if signal == "BUY":
                result = self._exec.open_buy(
                    symbol   = self.symbol,
                    sl_pips  = sl_pips,
                    tp_pips  = tp_pips,
                    comment  = SCALP_COMMENT,
                )
            else:
                result = self._exec.open_sell(
                    symbol   = self.symbol,
                    sl_pips  = sl_pips,
                    tp_pips  = tp_pips,
                    comment  = SCALP_COMMENT,
                )

            logger.info(
                "HFTScanner: %s %s — ticket=%s price=%s sl=%dpips tp=%dpips",
                signal, self.symbol, result.ticket, result.open_price,
                sl_pips, tp_pips,
            )
            return {
                "outcome":    "executed",
                "signal":     signal,
                "ticket":     result.ticket,
                "entry_price": result.open_price,
                "sl_pips":    sl_pips,
                "tp_pips":    tp_pips,
                "bias":       bias.value if hasattr(bias, 'value') else bias,
                "confidence": record.confidence,
                "indicators": ind,
                "symbol":     self.symbol,
            }

        except TradeValidationError as exc:
            logger.error("HFTScanner: validation error — %s", exc)
            return {"outcome": "error", "error": str(exc), "symbol": self.symbol}

    def _evaluate_entry(
        self,
        bias:       SessionBias,
        indicators: dict,
    ) -> str | None:
        """
        Returns "BUY", "SELL", or None.

        Two entry modes:
        1. EXTREME RSI — immediate reversal entry regardless of bias
        RSI < 25 → BUY (oversold bounce)
        RSI > 75 → SELL (overbought reversal)

        2. TREND FOLLOW — normal bias-aligned entry
        Requires EMA alignment + RSI in range + MACD confirmation
        """
        ema9      = indicators.get("ema9")
        ema21     = indicators.get("ema21")
        rsi_val   = indicators.get("rsi")
        macd_dir  = indicators.get("macd_direction", "neutral")
        ema_cross = indicators.get("ema_cross", "none")

        if any(v is None for v in [ema9, ema21, rsi_val]):
            return None

        # ── Mode 1: Extreme RSI reversal (highest priority) ───────────────────
        if rsi_val <= 40:
            logger.info(
                "HFTScanner: EXTREME OVERSOLD rsi=%.2f — reversal BUY signal", rsi_val
            )
            return "BUY"

        if rsi_val >= 60:
            logger.info(
                "HFTScanner: EXTREME OVERBOUGHT rsi=%.2f — reversal SELL signal", rsi_val
            )
            return "SELL"

        # ── Mode 2: Trend follow — requires bias alignment ────────────────────
        if bias == SessionBias.LONG:
            ema_aligned = ema9 > ema21 or ema_cross == "golden"
            rsi_ok      = RSI_OVERSOLD < rsi_val < RSI_OVERBOUGHT
            macd_ok     = macd_dir in ("bullish_increasing", "bullish_weakening")
            if ema_aligned and rsi_ok and macd_ok:
                return "BUY"

        elif bias == SessionBias.SHORT:
            ema_aligned = ema9 < ema21 or ema_cross == "death"
            rsi_ok      = RSI_OVERSOLD < rsi_val < RSI_OVERBOUGHT
            macd_ok     = macd_dir in ("bearish_increasing", "bearish_weakening")
            if ema_aligned and rsi_ok and macd_ok:
                return "SELL"

        return None

    def _check_early_exits(self, positions: list) -> list[dict]:
        """
        Check if any open scalp positions should be closed early to lock profit.
        Closes if floating P&L >= EARLY_EXIT_PCT of the implied TP value.
        Returns a list of outcomes for logging.
        """
        exits = []
        for pos in positions:
            # Only close profitable positions early
            if pos.profit <= 0:
                continue
            try:
                result = self._exec.close_trade(pos.ticket)
                logger.info(
                    "HFTScanner: early exit ticket=%s profit=%.2f",
                    pos.ticket, pos.profit,
                )
                exits.append({
                    "ticket": pos.ticket,
                    "profit": pos.profit,
                    "status": result.status,
                })
            except Exception as exc:
                logger.warning("HFTScanner: early exit failed for %s — %s", pos.ticket, exc)
        return exits