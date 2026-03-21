"""
forex/services/llm/providers.py
────────────────────────────────
Concrete LLM provider implementations for the Lykhan hard gate.

Each provider follows the same contract:
    call(prompt: str, timeout_seconds: int) -> LLMResponse

Providers are tried in this order by the gate:
    1. Groq      — free tier, ~300ms, Llama 3.3 70B
    2. Gemini    — free tier, ~800ms, Gemini 2.0 Flash
    3. Ollama    — local CPU, ~10-20s, last resort only

All providers parse the model's JSON response and return a structured
LLMResponse. If the model returns malformed JSON, the provider raises
ProviderError so the gate can fall through to the next one.

Setup requirements per provider
─────────────────────────────────
Groq:
    pip install groq
    Set GROQ_API_KEY in .env — get free key at console.groq.com

Gemini:
    pip install google-generativeai
    Set GEMINI_API_KEY in .env — get free key at aistudio.google.com

Ollama (offline fallback):
    Install: curl -fsSL https://ollama.ai/install.sh | sh
    Pull:    ollama pull deepseek-r1:7b
    Run:     ollama serve   (starts on localhost:11434)
    No API key needed — runs entirely on your machine.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)


# ── Shared response shape ─────────────────────────────────────────────────────

@dataclass
class LLMResponse:
    approved:    bool
    reasoning:   str
    confidence:  int         # 0-100
    model_used:  str
    latency_ms:  int


class ProviderError(Exception):
    """Raised when a provider fails and the gate should fall to the next one."""


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a risk management AI for an autonomous forex trading agent called Lykhan.
Your job is to evaluate incoming trade signals and decide whether to approve or reject each one.

You will receive a JSON object describing:
- The proposed trade (symbol, action, lot size, stop loss, take profit)
- Current account state (balance, equity, free margin, open trade count)
- Signal source and indicator readings (RSI, MACD, EMA crossovers, etc.)

Your decision criteria:
1. REJECT if free_margin is less than 3x the estimated margin for this trade
2. REJECT if open_trades_count >= 3 (max concurrent positions)
3. REJECT if the indicator readings strongly contradict the proposed direction
   (e.g. RSI > 75 on a BUY, RSI < 25 on a SELL)
4. REJECT if equity has dropped more than 10% below balance (drawdown guard)
5. APPROVE if indicators support the direction and account health is sound

Respond ONLY with a valid JSON object — no preamble, no markdown, no explanation outside the JSON:
{
  "approved": true or false,
  "reasoning": "One or two sentences explaining the decision",
  "confidence": integer 0 to 100
}"""


def _build_prompt(context: dict) -> str:
    """Format the trade context dict into the user message for the LLM."""
    return f"Evaluate this trade signal:\n{json.dumps(context, indent=2)}"


def _parse_response(raw: str, model_name: str, latency_ms: int) -> LLMResponse:
    """
    Parse the raw LLM text into an LLMResponse.
    Strips markdown code fences if the model adds them despite instructions.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1]) if len(lines) > 2 else cleaned
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"Model returned non-JSON response: {raw[:200]}") from exc

    approved   = bool(data.get("approved", False))
    reasoning  = str(data.get("reasoning", "No reasoning provided."))
    confidence = int(data.get("confidence", 50))

    return LLMResponse(
        approved   = approved,
        reasoning  = reasoning,
        confidence = max(0, min(100, confidence)),
        model_used = model_name,
        latency_ms = latency_ms,
    )


# ── Provider 1: Groq ──────────────────────────────────────────────────────────

class GroqProvider:
    """
    Primary provider. Free tier. Uses Llama 3.3 70B via Groq's inference API.
    Typical latency: 200-400ms. Rate limit: 30 req/min on free tier.

    Get your free API key: https://console.groq.com
    Set in .env: GROQ_API_KEY=gsk_...
    """

    MODEL = "llama-3.3-70b-versatile"

    def __init__(self) -> None:
        import os
        self._api_key = os.getenv("GROQ_API_KEY")
        if not self._api_key:
            raise ProviderError("GROQ_API_KEY not set in environment")

    def call(self, context: dict, timeout_seconds: int = 5) -> LLMResponse:
        try:
            from groq import Groq
        except ImportError as exc:
            raise ProviderError("groq package not installed. Run: pip install groq") from exc

        client = Groq(api_key=self._api_key, timeout=timeout_seconds)
        start = time.monotonic()
        try:
            response = client.chat.completions.create(
                model    = self.MODEL,
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": _build_prompt(context)},
                ],
                temperature = 0.1,   # Low temperature = more deterministic decisions
                max_tokens  = 200,
                response_format={"type": "json_object"},
            )
            latency_ms = int((time.monotonic() - start) * 1000)
            raw = response.choices[0].message.content
            return _parse_response(raw, f"groq/{self.MODEL}", latency_ms)
        except Exception as exc:
            raise ProviderError(f"Groq API call failed: {exc}") from exc


# ── Provider 2: Gemini Flash ──────────────────────────────────────────────────

class GeminiProvider:
    """
    First fallback. Free tier. Uses Gemini 2.0 Flash.
    Typical latency: 600-1200ms. Free tier: 1500 req/day, 15 req/min.

    Get your free API key: https://aistudio.google.com
    Set in .env: GEMINI_API_KEY=AIza...
    """

    MODEL = "gemini-2.0-flash"

    def __init__(self) -> None:
        import os
        self._api_key = os.getenv("GEMINI_API_KEY")
        if not self._api_key:
            raise ProviderError("GEMINI_API_KEY not set in environment")

    def call(self, context: dict, timeout_seconds: int = 10) -> LLMResponse:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise ProviderError("google-genai not installed. Run: pip install google-genai") from exc

        client = genai.Client(api_key=self._api_key)

        start = time.monotonic()
        try:
            response = client.models.generate_content(
                model    = "gemini-2.0-flash",
                contents = _build_prompt(context),
                config   = types.GenerateContentConfig(
                    system_instruction = SYSTEM_PROMPT,
                    temperature        = 0.1,
                    max_output_tokens  = 200,
                ),
            )
            latency_ms = int((time.monotonic() - start) * 1000)
            raw = response.text
            return _parse_response(raw, f"gemini/{self.MODEL}", latency_ms)
        except Exception as exc:
            raise ProviderError(f"Gemini API call failed: {exc}") from exc

# ── Provider 3: Ollama (offline fallback) ───────────────────────────────────
class OllamaProvider:
    """
    Last-resort offline fallback. Runs locally via Ollama on CPU.

    WARNING: On 8GB RAM with CPU-only inference, expect 10-20 seconds
    per call with deepseek-r1:7b or qwen2.5:7b. This provider exists so
    the system keeps functioning if both Groq and Gemini are down, but it
    will significantly slow the trade pipeline when activated.

    Setup:
        curl -fsSL https://ollama.ai/install.sh | sh
        ollama pull deepseek-r1:7b   # ~4.5GB download, ~4.5GB RAM
        ollama serve                  # starts on localhost:11434

    No API key required.
    """

    BASE_URL = "http://localhost:11434/api/chat"
    MODEL    = "deepseek-r1:7b"   # Change to qwen2.5:7b if you prefer

    def call(self, context: dict, timeout_seconds: int = 30) -> LLMResponse:
        try:
            import urllib.request
        except ImportError as exc:
            raise ProviderError("urllib not available") from exc

        payload = {
            "model": self.MODEL,
            "stream": False,
            "options": {"temperature": 0.1},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": _build_prompt(context)},
            ],
        }

        start = time.monotonic()
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.BASE_URL,
                data    = data,
                headers = {"Content-Type": "application/json"},
                method  = "POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            latency_ms = int((time.monotonic() - start) * 1000)
            raw = result["message"]["content"]
            return _parse_response(raw, f"ollama/{self.MODEL}", latency_ms)
        except Exception as exc:
            raise ProviderError(f"Ollama call failed (is `ollama serve` running?): {exc}") from exc