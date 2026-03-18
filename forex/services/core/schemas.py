"""
forex/services/core/schemas.py
──────────────────────────────
Pydantic domain models for all trade-related data structures.

MT5 DateTime Format Note
─────────────────────────
MT5 writes datetime strings as "2026.03.15 08:07:31" (dots as date separators).
Every datetime field has a field_validator that converts this to ISO 8601 before
Pydantic parses it.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ── Enumerations ──────────────────────────────────────────────────────────────

class TradeAction(str, Enum):
    BUY         = "BUY"
    SELL        = "SELL"
    CLOSE       = "CLOSE"
    CLOSE_ALL   = "CLOSE_ALL"
    GET_STATUS  = "GET_STATUS"
    GET_CANDLES = "GET_CANDLES"


class TradeStatus(str, Enum):
    PENDING  = "PENDING"
    EXECUTED = "EXECUTED"
    REJECTED = "REJECTED"
    CLOSED   = "CLOSED"
    ERROR    = "ERROR"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT  = "LIMIT"
    STOP   = "STOP"


class Timeframe(str, Enum):
    M1  = "M1"
    M5  = "M5"
    M15 = "M15"
    H1  = "H1"
    H4  = "H4"
    D1  = "D1"
    W1  = "W1"


class SessionBias(str, Enum):
    """
    The directional bias produced by the strategic LLM analysis every 30 minutes.
    The HFT scanner reads this from Redis and only fires trades that align with it.
    NEUTRAL means the LLM sees conflicting signals — HFT scanner pauses.
    """
    LONG    = "LONG"
    SHORT   = "SHORT"
    NEUTRAL = "NEUTRAL"


# ── Shared datetime converter ─────────────────────────────────────────────────

def _parse_mt5_dt(v: object) -> object:
    """Convert MT5 "2026.03.15 08:07:31" → "2026-03-15T08:07:31" for Pydantic."""
    if isinstance(v, str):
        return v.replace('.', '-', 2).replace(' ', 'T')
    return v


# ── OHLCV Candle Models ───────────────────────────────────────────────────────

class CandleBar(BaseModel):
    """A single OHLCV candlestick bar returned from the MT5 EA."""
    time:   datetime
    open:   float
    high:   float
    low:    float
    close:  float
    volume: int

    @field_validator('time', mode='before')
    @classmethod
    def parse_time(cls, v: object) -> object:
        return _parse_mt5_dt(v)

    @property
    def body_size(self) -> float:
        """Absolute size of the candle body in price units."""
        return abs(self.close - self.open)

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open


class CandleData(BaseModel):
    """A series of CandleBar objects for a given symbol and timeframe."""
    symbol:     str
    timeframe:  str
    bars:       list[CandleBar]
    fetched_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def closes(self) -> list[float]:
        return [b.close for b in self.bars]

    @property
    def highs(self) -> list[float]:
        return [b.high for b in self.bars]

    @property
    def lows(self) -> list[float]:
        return [b.low for b in self.bars]

    @property
    def volumes(self) -> list[int]:
        return [b.volume for b in self.bars]

    @property
    def latest(self) -> CandleBar | None:
        return self.bars[-1] if self.bars else None


# ── Command: Python → MT5 ─────────────────────────────────────────────────────

class TradeCommand(BaseModel):
    """
    A single instruction written as JSON into the bridge commands/ directory.
    The LykhanBridge EA reads this, executes the trade, and writes a result.
    """
    command_id: str           = Field(default_factory=lambda: str(uuid.uuid4()))
    action:     TradeAction
    symbol:     str           = Field(default="EURUSD")
    lot_size:   float         = Field(default=0.01, ge=0.01, le=100.0)
    order_type: OrderType     = Field(default=OrderType.MARKET)
    price:      float         = Field(default=0.0)
    sl_pips:    int           = Field(default=0)
    tp_pips:    int           = Field(default=0)
    slippage:   int           = Field(default=10)
    magic:      int           = Field(default=20240101)
    comment:    str           = Field(default="lykhan-forex")
    ticket:     Optional[int] = Field(default=None)
    timeframe:  str           = Field(default="H1")
    count:      int           = Field(default=100)
    timestamp:  datetime      = Field(default_factory=datetime.utcnow)

    model_config = {"use_enum_values": True}


# ── Result: MT5 → Python ──────────────────────────────────────────────────────

class TradeResult(BaseModel):
    """The outcome written by the MQL5 EA after processing a TradeCommand."""
    command_id:    str
    status:        TradeStatus
    ticket:        Optional[int]   = None
    open_price:    Optional[float] = None
    close_price:   Optional[float] = None
    profit:        Optional[float] = None
    error_code:    Optional[int]   = None
    error_message: Optional[str]   = None
    processed_at:  Optional[datetime] = None

    model_config = {"use_enum_values": True}

    @field_validator('processed_at', mode='before')
    @classmethod
    def parse_processed_at(cls, v: object) -> object:
        return _parse_mt5_dt(v)


# ── Live Position ─────────────────────────────────────────────────────────────

class Position(BaseModel):
    """A single open trade position returned inside an AccountSnapshot."""
    ticket:        int
    symbol:        str
    action:        str
    lot_size:      float
    open_price:    float
    current_price: float
    sl:            float
    tp:            float
    profit:        float
    swap:          float
    magic:         int
    comment:       str
    open_time:     datetime

    @field_validator('open_time', mode='before')
    @classmethod
    def parse_open_time(cls, v: object) -> object:
        return _parse_mt5_dt(v)


# ── Account Snapshot ──────────────────────────────────────────────────────────

class AccountSnapshot(BaseModel):
    """A complete picture of the trading account at a single point in time."""
    balance:       float
    equity:        float
    margin:        float
    free_margin:   float
    margin_level:  float
    profit:        float
    positions:     list[Position] = Field(default_factory=list)
    snapshot_time: datetime       = Field(default_factory=datetime.utcnow)

    @field_validator('snapshot_time', mode='before')
    @classmethod
    def parse_snapshot_time(cls, v: object) -> object:
        return _parse_mt5_dt(v)

    @property
    def open_trade_count(self) -> int:
        return len(self.positions)

    @property
    def total_floating_pnl(self) -> float:
        return sum(p.profit for p in self.positions)

    @property
    def drawdown_pct(self) -> float:
        if self.balance <= 0:
            return 0.0
        return round((1 - self.equity / self.balance) * 100, 2)