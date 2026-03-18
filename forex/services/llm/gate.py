"""
forex/services/llm/gate.py
───────────────────────────
The LLM hard gate — the single entry point for all trade approval decisions.

How the fallback chain works
─────────────────────────────
1. Try Groq (5s timeout). If it responds → return decision immediately.
2. If Groq fails (API error, timeout, key missing) → try Gemini (10s timeout).
3. If Gemini fails → try Ollama (30s timeout, local CPU).
4. If ALL providers fail → return a rejected GateDecision with
   model_used="all_providers_failed". No trade executes. This is intentional:
   you chose a hard gate with no fallback execution path.

Every decision — including provider failures — is returned as a structured
GateDecision so the caller (the Celery task) always has a consistent object
to write to TradeLog regardless of what happened.

The gate knows nothing about Django, FileBridge, or Celery. It is a pure
Python service that takes a dict of trade context and returns a decision.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from forex.services.llm.providers import (
    GroqProvider,
    GeminiProvider,
    OllamaProvider,
    LLMResponse,
    ProviderError,
)

logger = logging.getLogger(__name__)


@dataclass
class GateDecision:
    """
    The structured outcome of the LLM gate evaluation.
    Always returned — never raises — so the Celery task can always write
    a complete TradeLog row regardless of provider failures.
    """
    approved:    bool
    reasoning:   str
    confidence:  int
    model_used:  str
    latency_ms:  int

    @classmethod
    def from_llm_response(cls, resp: LLMResponse) -> "GateDecision":
        return cls(
            approved   = resp.approved,
            reasoning  = resp.reasoning,
            confidence = resp.confidence,
            model_used = resp.model_used,
            latency_ms = resp.latency_ms,
        )

    @classmethod
    def all_providers_failed(cls, total_ms: int) -> "GateDecision":
        return cls(
            approved   = False,
            reasoning  = "All LLM providers failed. Hard gate requires LLM approval — trade rejected for safety.",
            confidence = 0,
            model_used = "all_providers_failed",
            latency_ms = total_ms,
        )


class LLMGate:
    """
    Evaluates a trade signal through the provider fallback chain and returns
    a GateDecision. Never raises — all exceptions are caught and converted
    into a rejected GateDecision with appropriate reasoning.

    Usage:
        gate = LLMGate()
        decision = gate.evaluate(context)
        if decision.approved:
            executor.open_buy(...)
    """

    def evaluate(self, context: dict) -> GateDecision:
        """
        Run the trade context through the provider chain.

        :param context: Dict with keys like symbol, action, lot_size,
                        sl_pips, tp_pips, balance, equity, free_margin,
                        open_trades_count, indicators (sub-dict).
        :returns: GateDecision with approved/rejected + full reasoning.
        """
        import time
        overall_start = time.monotonic()

        providers = [
            ("Groq",   self._try_groq),
            ("Gemini", self._try_gemini),
            ("Ollama", self._try_ollama),
        ]

        for name, provider_fn in providers:
            try:
                logger.info("LLM gate: trying provider %s", name)
                response = provider_fn(context)
                logger.info(
                    "LLM gate: %s responded in %dms — approved=%s confidence=%d",
                    response.model_used, response.latency_ms,
                    response.approved, response.confidence,
                )
                return GateDecision.from_llm_response(response)
            except ProviderError as exc:
                logger.warning("LLM gate: %s failed — %s — trying next provider", name, exc)
            except Exception as exc:
                logger.error("LLM gate: unexpected error from %s — %s", name, exc, exc_info=True)

        total_ms = int((time.monotonic() - overall_start) * 1000)
        logger.error("LLM gate: all providers failed after %dms — rejecting trade", total_ms)
        return GateDecision.all_providers_failed(total_ms)

    # ── Private provider callers ───────────────────────────────────────────────

    def _try_groq(self, context: dict) -> LLMResponse:
        return GroqProvider().call(context, timeout_seconds=5)

    def _try_gemini(self, context: dict) -> LLMResponse:
        return GeminiProvider().call(context, timeout_seconds=10)

    def _try_ollama(self, context: dict) -> LLMResponse:
        return OllamaProvider().call(context, timeout_seconds=30)


def build_gate_context(
    symbol: str,
    action: str,
    lot_size: float,
    sl_pips: int,
    tp_pips: int,
    balance: float,
    equity: float,
    free_margin: float,
    open_trades_count: int,
    indicators: dict | None = None,
    signal_source: str = "tradingview",
    comment: str = "",
) -> dict:
    """
    Build the context dict that gets serialised into the LLM prompt.
    Keeping this as a standalone function (not a method) makes it easy to
    unit-test the context shape without instantiating the gate or mocking
    any provider.
    """
    drawdown_pct = round((1 - equity / balance) * 100, 2) if balance > 0 else 0.0

    return {
        "trade": {
            "symbol":    symbol,
            "action":    action,
            "lot_size":  lot_size,
            "sl_pips":   sl_pips,
            "tp_pips":   tp_pips,
            "comment":   comment,
        },
        "account": {
            "balance":           round(balance, 2),
            "equity":            round(equity, 2),
            "free_margin":       round(free_margin, 2),
            "drawdown_pct":      drawdown_pct,
            "open_trades_count": open_trades_count,
        },
        "signal": {
            "source":     signal_source,
            "indicators": indicators or {},
        },
    }