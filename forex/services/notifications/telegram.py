"""
forex/services/notifications/telegram.py
──────────────────────────────────────────
Telegram notification service for Lykhan.

Sends messages to your personal Telegram DM via the Bot API.
Uses only Python stdlib urllib — no extra package required.

Notification types
───────────────────
- Trade opened:   sent immediately when any position opens (TV signal or HFT)
- Trade closed:   sent immediately when a position closes
- 30-min summary: periodic digest — trade count, net P&L, account status
- Critical alert: drawdown breach, all LLM providers down, MT5 bridge offline

Setup (if not done already)
─────────────────────────────
1. Open Telegram → search @BotFather → /newbot → follow prompts
2. Copy the token it gives you → TELEGRAM_BOT_TOKEN in .env
3. Message your new bot once (to open the chat)
4. Open Telegram → search @userinfobot → message it → copy your ID
5. Add TELEGRAM_CHAT_ID=<your_id> to .env

Message formatting uses Telegram's MarkdownV2. Special characters are
escaped automatically by _escape().
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_BASE = "https://api.telegram.org/bot{token}/sendMessage"


def _escape(text: str) -> str:
    """Escape MarkdownV2 special characters."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def _pnl_emoji(value: float) -> str:
    if value > 0:
        return "✅"
    if value < 0:
        return "🔴"
    return "⚪"


class TelegramNotifier:
    """
    Sends formatted Telegram messages to a single chat (your personal DM).

    All methods are fire-and-forget — they log errors but never raise,
    so a Telegram failure never blocks the trade pipeline.

    Usage:
        notifier = TelegramNotifier()
        notifier.send_trade_opened(symbol="EURUSD", action="BUY",
                                   ticket=123456, price=1.08234,
                                   sl_pips=30, tp_pips=60, source="hft")
    """

    def __init__(
        self,
        token:   Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> None:
        self._token   = token   or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")

        if not self._token or not self._chat_id:
            logger.warning(
                "TelegramNotifier: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — "
                "notifications disabled."
            )

    # ── Public notification methods ───────────────────────────────────────────

    def send_trade_opened(
        self,
        symbol:   str,
        action:   str,
        ticket:   int | None,
        price:    float | None,
        sl_pips:  int,
        tp_pips:  int,
        source:   str = "signal",   # "signal", "hft", "manual"
    ) -> None:
        direction = "📈 *LONG*" if action == "BUY" else "📉 *SHORT*"
        tag       = {"signal": "TV Signal", "hft": "HFT Scalp", "manual": "Manual"}.get(source, source)
        text = (
            f"🤖 *Lykhan — Trade Opened*\n\n"
            f"Pair:       `{_escape(symbol)}`\n"
            f"Direction:  {direction}\n"
            f"Entry:      `{price:.5f}`\n"
            f"Ticket:     `{ticket}`\n"
            f"SL:         `{sl_pips}` pips\n"
            f"TP:         `{tp_pips}` pips\n"
            f"Source:     {_escape(tag)}\n"
            f"Time:       {_escape(datetime.now(timezone.utc).strftime('%H:%M:%S UTC'))}"
        )
        self._send(text)

    def send_trade_closed(
        self,
        symbol:     str,
        action:     str,
        ticket:     int | None,
        open_price: float | None,
        close_price: float | None,
        profit:     float,
        source:     str = "signal",
    ) -> None:
        emoji = _pnl_emoji(profit)
        sign  = "+" if profit >= 0 else ""
        text = (
            f"{emoji} *Lykhan — Trade Closed*\n\n"
            f"Pair:        `{_escape(symbol)}`\n"
            f"Direction:   `{action}`\n"
            f"Entry:       `{open_price:.5f}`\n"
            f"Exit:        `{close_price:.5f}`\n"
            f"P&L:         `{sign}{profit:.2f}`\n"
            f"Ticket:      `{ticket}`\n"
            f"Time:        {_escape(datetime.now(timezone.utc).strftime('%H:%M:%S UTC'))}"
        )
        self._send(text)

    def send_30min_summary(
        self,
        balance:       float,
        equity:        float,
        open_trades:   int,
        trades_count:  int,
        net_pnl:       float,
        session_bias:  str,
        bias_confidence: int,
    ) -> None:
        equity_change = equity - balance
        sign          = "+" if net_pnl >= 0 else ""
        eq_sign       = "+" if equity_change >= 0 else ""
        bias_map      = {"LONG": "📈 Long", "SHORT": "📉 Short", "NEUTRAL": "⚪ Neutral"}
        bias_str      = bias_map.get(session_bias, session_bias)

        text = (
            f"📊 *Lykhan — 30min Summary*\n\n"
            f"Balance:     `{balance:.2f}`\n"
            f"Equity:      `{equity:.2f}` \\({eq_sign}{equity_change:.2f}\\)\n"
            f"Open trades: `{open_trades}`\n"
            f"Trades run:  `{trades_count}` this period\n"
            f"Net P&L:     `{sign}{net_pnl:.2f}`\n"
            f"Session bias: {bias_str} \\(`{bias_confidence}%` confidence\\)\n"
            f"Time:        {_escape(datetime.now(timezone.utc).strftime('%H:%M UTC'))}"
        )
        self._send(text)

    def send_critical_alert(self, title: str, detail: str) -> None:
        text = (
            f"🚨 *Lykhan — Critical Alert*\n\n"
            f"*{_escape(title)}*\n"
            f"{_escape(detail)}\n"
            f"Time: {_escape(datetime.now(timezone.utc).strftime('%H:%M:%S UTC'))}"
        )
        self._send(text)

    def send_session_bias_update(
        self,
        symbol:     str,
        bias:       str,
        confidence: int,
        reasoning:  str,
        model_used: str,
    ) -> None:
        """Sent after each 30-min strategic LLM analysis completes."""
        bias_map = {"LONG": "📈 LONG", "SHORT": "📉 SHORT", "NEUTRAL": "⚪ NEUTRAL"}
        text = (
            f"🧠 *Lykhan — Session Bias Updated*\n\n"
            f"Pair:       `{_escape(symbol)}`\n"
            f"Bias:       {bias_map.get(bias, bias)}\n"
            f"Confidence: `{confidence}%`\n"
            f"Model:      `{_escape(model_used)}`\n\n"
            f"_{_escape(reasoning[:200])}_"
        )
        self._send(text)

    def send_llm_gate_decision(
        self,
        symbol:     str,
        action:     str,
        approved:   bool,
        reasoning:  str,
        model_used: str,
        confidence: int,
    ) -> None:
        """Sent when the LLM hard gate approves or rejects a TradingView signal."""
        status = "✅ *APPROVED*" if approved else "❌ *REJECTED*"
        text = (
            f"🤖 *LLM Gate Decision*\n\n"
            f"Signal:     `{action} {_escape(symbol)}`\n"
            f"Decision:   {status}\n"
            f"Confidence: `{confidence}%`\n"
            f"Model:      `{_escape(model_used)}`\n\n"
            f"_{_escape(reasoning[:200])}_"
        )
        self._send(text)

    # ── Internal sender ───────────────────────────────────────────────────────

    def _send(self, text: str) -> None:
        """Send a MarkdownV2 message via the Telegram Bot API."""
        if not self._token or not self._chat_id:
            logger.debug("TelegramNotifier: skipping (not configured)")
            return

        url     = _BASE.format(token=self._token)
        payload = json.dumps({
            "chat_id":    self._chat_id,
            "text":       text,
            "parse_mode": "MarkdownV2",
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data    = payload,
            headers = {"Content-Type": "application/json"},
            method  = "POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    logger.warning("TelegramNotifier: HTTP %d", resp.status)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            logger.error("TelegramNotifier: HTTP error %d — %s", exc.code, body[:200])
        except Exception as exc:
            logger.error("TelegramNotifier: send failed — %s", exc)