"""
forex/services/llm/chart_analyzer.py
──────────────────────────────────────
Strategic LLM chart analysis — the brain of the 30-minute session loop.

Every 30 minutes, ChartAnalyzer receives OHLCV data across M5/H1/D1/W1
timeframes, builds a compact market summary, and sends it to the LLM
provider chain (Groq → Gemini → Ollama). The LLM returns a directional
bias: LONG, SHORT, or NEUTRAL.

This bias is cached in Redis via SessionBiasCache and read by the HFT
scanner on every M1 tick. The LLM is never called on the hot path —
it only runs every 30 minutes on a background Celery Beat task.

The analysis prompt is designed to produce a structured JSON response
that includes the bias, confidence (0-100), detailed reasoning, and
key levels to watch. This reasoning is stored in Redis and shown on
the live dashboard under "LLM analysis feed".
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from forex.services.core.schemas import CandleData, SessionBias
from forex.services.market.ohlcv import compute_indicators
from forex.services.llm.providers import (
    GroqProvider,
    GeminiProvider,
    OllamaProvider,
    ProviderError,
)

logger = logging.getLogger(__name__)


CHART_ANALYSIS_SYSTEM_PROMPT = """You are a professional forex market analyst specialising in EURUSD intraday scalping.

You will receive OHLCV candlestick data and computed technical indicators across multiple timeframes:
  - W1 (weekly): macro trend direction
  - D1 (daily): medium-term bias and key levels
  - H1 (hourly): session structure and momentum
  - M5 (5-minute): current intraday momentum

Your job is to synthesise all timeframes and produce a directional bias for the next 30 minutes.

Decision rules:
1. If H1 and D1 align in direction AND M5 confirms → strong bias (confidence 75-95)
2. If H1 and D1 align but M5 contradicts → moderate bias (confidence 50-74)
3. If H1 contradicts D1 → NEUTRAL (conflicting timeframes, pause trading)
4. RSI above 70 on H1 = overbought → avoid LONG bias
5. RSI below 30 on H1 = oversold → avoid SHORT bias
6. MACD histogram increasing on H1 = momentum confirmation
7. EMA golden cross on H1 = bullish confirmation
8. EMA death cross on H1 = bearish confirmation

You MUST respond with ONLY valid JSON — no preamble, no markdown:
{
  "bias": "LONG" or "SHORT" or "NEUTRAL",
  "confidence": integer 0 to 100,
  "reasoning": "2-3 sentences explaining the multi-timeframe synthesis",
  "key_support": float or null,
  "key_resistance": float or null,
  "recommended_sl_pips": integer,
  "recommended_tp_pips": integer
}"""


@dataclass
class AnalysisResult:
    """Structured result from the LLM chart analysis."""
    bias:                 SessionBias
    confidence:           int
    reasoning:            str
    model_used:           str
    latency_ms:           int
    key_support:          float | None
    key_resistance:       float | None
    recommended_sl_pips:  int
    recommended_tp_pips:  int

    @classmethod
    def neutral_fallback(cls, latency_ms: int, reason: str) -> "AnalysisResult":
        return cls(
            bias                = SessionBias.NEUTRAL,
            confidence          = 0,
            reasoning           = reason,
            model_used          = "fallback",
            latency_ms          = latency_ms,
            key_support         = None,
            key_resistance      = None,
            recommended_sl_pips = 50,
            recommended_tp_pips = 100,
        )


class ChartAnalyzer:
    """
    Sends multi-timeframe OHLCV indicator summaries to the LLM provider
    chain and returns a structured AnalysisResult.

    The provider chain is the same as the trade gate: Groq → Gemini → Ollama.
    Failure of all three returns a NEUTRAL fallback so the HFT scanner
    pauses rather than trading blind.
    """

    def analyze(
        self,
        symbol:         str,
        candle_data:    dict[str, CandleData],
    ) -> AnalysisResult:
        """
        :param symbol:      e.g. "EURUSD"
        :param candle_data: dict mapping timeframe string to CandleData
        :returns: AnalysisResult with bias, confidence, reasoning, levels
        """
        if not candle_data:
            return AnalysisResult.neutral_fallback(0, "No OHLCV data available.")

        prompt   = self._build_prompt(symbol, candle_data)
        start    = time.monotonic()
        providers = [
            ("Groq",   self._try_groq),
            ("Gemini", self._try_gemini),
            ("Ollama", self._try_ollama),
        ]

        for name, fn in providers:
            try:
                logger.info("ChartAnalyzer: trying %s", name)
                raw, model_used = fn(prompt)
                latency_ms = int((time.monotonic() - start) * 1000)
                result = self._parse_response(raw, model_used, latency_ms)
                logger.info(
                    "ChartAnalyzer: %s → bias=%s confidence=%d in %dms",
                    model_used, result.bias.value, result.confidence, latency_ms,
                )
                return result
            except ProviderError as exc:
                logger.warning("ChartAnalyzer: %s failed — %s", name, exc)
            except Exception as exc:
                logger.error("ChartAnalyzer: unexpected %s error — %s", name, exc, exc_info=True)

        total_ms = int((time.monotonic() - start) * 1000)
        logger.error("ChartAnalyzer: all providers failed — returning NEUTRAL")
        return AnalysisResult.neutral_fallback(
            total_ms,
            "All LLM providers failed. Defaulting to NEUTRAL — HFT scanner paused.",
        )

    def _build_prompt(self, symbol: str, candle_data: dict[str, CandleData]) -> str:
        """
        Build a compact indicator summary across all timeframes.
        We send indicators, not raw OHLCV arrays, to keep the prompt short
        and well within token limits even on free tiers.
        """
        tf_summaries = {}
        for tf, data in candle_data.items():
            indicators = compute_indicators(data)
            tf_summaries[tf] = indicators

        payload = {
            "symbol":     symbol,
            "timeframes": tf_summaries,
        }
        return f"Analyse this multi-timeframe indicator summary:\n{json.dumps(payload, indent=2)}"

    def _parse_response(
        self,
        raw:        str,
        model_used: str,
        latency_ms: int,
    ) -> AnalysisResult:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines   = cleaned.split("\n")
            cleaned = "\n".join(lines[1:-1]) if len(lines) > 2 else cleaned

        data = json.loads(cleaned)

        bias_str = str(data.get("bias", "NEUTRAL")).upper()
        if bias_str not in ("LONG", "SHORT", "NEUTRAL"):
            bias_str = "NEUTRAL"

        return AnalysisResult(
            bias                = SessionBias(bias_str),
            confidence          = max(0, min(100, int(data.get("confidence", 50)))),
            reasoning           = str(data.get("reasoning", "")),
            model_used          = model_used,
            latency_ms          = latency_ms,
            key_support         = data.get("key_support"),
            key_resistance      = data.get("key_resistance"),
            recommended_sl_pips = int(data.get("recommended_sl_pips", 50)),
            recommended_tp_pips = int(data.get("recommended_tp_pips", 100)),
        )

    def _try_groq(self, prompt: str) -> tuple[str, str]:
        provider = GroqProvider()
        import os
        from groq import Groq
        client = Groq(api_key=os.getenv("GROQ_API_KEY"), timeout=10)
        resp = client.chat.completions.create(
            model       = "llama-3.3-70b-versatile",
            messages    = [
                {"role": "system", "content": CHART_ANALYSIS_SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            temperature     = 0.1,
            max_tokens      = 300,
            response_format = {"type": "json_object"},
        )
        return resp.choices[0].message.content, "groq/llama-3.3-70b-versatile"

    def _try_gemini(self, prompt: str) -> tuple[str, str]:
        import os, google.generativeai as genai
        genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
        model = genai.GenerativeModel(
            model_name         = "gemini-2.0-flash",
            system_instruction = CHART_ANALYSIS_SYSTEM_PROMPT,
            generation_config  = genai.GenerationConfig(
                temperature        = 0.1,
                max_output_tokens  = 300,
                response_mime_type = "application/json",
            ),
        )
        resp = model.generate_content(prompt, request_options={"timeout": 15})
        return resp.text, "gemini/gemini-2.0-flash"

    def _try_ollama(self, prompt: str) -> tuple[str, str]:
        import urllib.request
        payload = json.dumps({
            "model":   "deepseek-r1:7b",
            "stream":  False,
            "options": {"temperature": 0.1},
            "messages": [
                {"role": "system", "content": CHART_ANALYSIS_SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
        }).encode("utf-8")
        req = urllib.request.Request(
            "http://localhost:11434/api/chat",
            data    = payload,
            headers = {"Content-Type": "application/json"},
            method  = "POST",
        )
        with urllib.request.urlopen(req, timeout=45) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["message"]["content"], "ollama/deepseek-r1:7b"