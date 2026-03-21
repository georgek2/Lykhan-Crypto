"""
forex/services/core/market_hours.py
─────────────────────────────────────
Market hours checker. Tells the HFT scanner and strategic analysis
whether a given symbol is currently tradeable.
"""
from __future__ import annotations

from datetime import datetime, timezone


ALWAYS_OPEN = {
    "BTCUSD", "ETHUSD", "XRPUSD", "LTCUSD", "BCHUSD",
    "R_10", "R_25", "R_50", "R_75", "R_100",
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
}

FOREX_SYMBOLS = {
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
    "USDCAD", "USDCHF", "NZDUSD", "GBPJPY",
}


def is_market_open(symbol: str) -> bool:
    symbol = symbol.upper()

    if symbol in ALWAYS_OPEN:
        return True

    now = datetime.now(timezone.utc)
    weekday = now.weekday()  # 0=Monday, 6=Sunday

    if weekday == 5:
        return False
    if weekday == 6:
        return False
    if weekday == 4 and now.hour >= 21:
        return False

    return True


def get_closed_reason(symbol: str) -> str:
    now = datetime.now(timezone.utc)
    weekday = now.weekday()

    if weekday == 5:
        return f"{symbol} — forex market closed (Saturday)"
    if weekday == 6:
        return f"{symbol} — forex market closed (Sunday)"
    if weekday == 4 and now.hour >= 21:
        return f"{symbol} — forex market closed (Friday after 21:00 UTC)"
    return f"{symbol} — market closed"