"""
forex/services/market/bias_cache.py
─────────────────────────────────────
Redis wrapper for the LLM strategic session bias.

The strategic loop runs every 30 minutes, calls the LLM with OHLCV data,
and stores the result here. The HFT scanner reads it on every tick — a
Redis GET takes ~0.1ms, adding zero meaningful latency to the hot path.

The bias expires automatically after TTL_SECONDS (default 35 minutes,
slightly longer than the 30-min update cycle to handle a late update).
If the key is expired or missing when the HFT scanner reads it, the scanner
enters NEUTRAL mode and skips all trades until the next LLM analysis runs.

Key format: lykhan:bias:<SYMBOL>  e.g. lykhan:bias:EURUSD
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from forex.services.core.schemas import SessionBias

logger = logging.getLogger(__name__)

TTL_SECONDS = 35 * 60   # 35 minutes — slightly longer than the 30min update cycle
REDIS_KEY_PREFIX = "lykhan:bias:"


class BiasRecord:
    """The full bias record stored in Redis, not just the enum value."""

    def __init__(
        self,
        bias:        SessionBias,
        confidence:  int,
        reasoning:   str,
        model_used:  str,
        symbol:      str,
        set_at:      datetime | None = None,
    ) -> None:
        self.bias       = bias
        self.confidence = confidence
        self.reasoning  = reasoning
        self.model_used = model_used
        self.symbol     = symbol
        self.set_at     = set_at or datetime.now(timezone.utc)

    def to_json(self) -> str:
        return json.dumps({
            "bias":       self.bias.value if hasattr(self.bias, 'value') else self.bias,
            "confidence": self.confidence,
            "reasoning":  self.reasoning,
            "model_used": self.model_used,
            "symbol":     self.symbol,
            "set_at":     self.set_at.isoformat(),
        })

    @classmethod
    def from_json(cls, raw: str) -> "BiasRecord":
        data = json.loads(raw)
        return cls(
            bias       = SessionBias(data["bias"]),
            confidence = data["confidence"],
            reasoning  = data["reasoning"],
            model_used = data["model_used"],
            symbol     = data["symbol"],
            set_at     = datetime.fromisoformat(data["set_at"]),
        )


class SessionBiasCache:
    """
    Reads and writes the LLM session bias to Redis.

    Usage:
        cache = SessionBiasCache()
        cache.set("EURUSD", SessionBias.LONG, confidence=82, reasoning="...", model="groq/llama-3.3-70b")
        record = cache.get("EURUSD")
        if record is None:
            # No bias set yet — HFT scanner should pause
    """

    def __init__(self, redis_client=None) -> None:
        # Accept an injected client for testing. Otherwise create one lazily.
        self._redis = redis_client

    def _get_client(self):
        """Lazy Redis connection — don't connect at import time."""
        if self._redis is None:
            import redis as redis_lib
            from forex.services.config.settings import forex_settings
            url = getattr(forex_settings, 'redis_url', 'redis://localhost:6379/0')
            self._redis = redis_lib.from_url(url, decode_responses=True)
        return self._redis

    def set(
        self,
        symbol:     str,
        bias:       SessionBias,
        confidence: int,
        reasoning:  str,
        model_used: str,
        ttl:        int = TTL_SECONDS,
    ) -> None:
        """Write the session bias to Redis with a TTL."""
        key    = f"{REDIS_KEY_PREFIX}{symbol.upper()}"
        record = BiasRecord(
            bias       = bias,
            confidence = confidence,
            reasoning  = reasoning,
            model_used = model_used,
            symbol     = symbol.upper(),
        )
        try:
            self._get_client().setex(key, ttl, record.to_json())
            logger.info(
                "BiasCache: set %s bias=%s confidence=%d ttl=%ds",
                symbol, bias.value if hasattr(bias, 'value') else bias,
                confidence, ttl,
            )
        except Exception as exc:
            logger.error("BiasCache: failed to write to Redis — %s", exc)

    def get(self, symbol: str) -> Optional[BiasRecord]:
        """
        Read the current session bias for a symbol.
        Returns None if the key is missing or expired — the HFT scanner
        must treat this as NEUTRAL and skip trading.
        """
        key = f"{REDIS_KEY_PREFIX}{symbol.upper()}"
        try:
            raw = self._get_client().get(key)
            if raw is None:
                return None
            return BiasRecord.from_json(raw)
        except Exception as exc:
            logger.error("BiasCache: failed to read from Redis — %s", exc)
            return None

    def clear(self, symbol: str) -> None:
        """Explicitly clear the bias for a symbol (e.g. after a drawdown breach)."""
        key = f"{REDIS_KEY_PREFIX}{symbol.upper()}"
        try:
            self._get_client().delete(key)
            logger.info("BiasCache: cleared %s", symbol)
        except Exception as exc:
            logger.error("BiasCache: failed to clear — %s", exc)

    def get_bias_value(self, symbol: str) -> SessionBias:
        """
        Convenience method: returns just the SessionBias enum value.
        Returns SessionBias.NEUTRAL if no record exists.
        """
        record = self.get(symbol)
        if record is None:
            return SessionBias.NEUTRAL
        return record.bias