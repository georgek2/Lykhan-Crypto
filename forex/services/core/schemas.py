"""
forex/services/core/schemas.py
──────────────────────────────
Pydantic domain models for all trade-related data structures.

Named schemas.py rather than models.py to avoid confusion with Django's
ORM models. Django models talk to the database — these Pydantic schemas
validate and describe the shape of data flowing between Python and MT5.

MT5 DateTime Format Note
─────────────────────────
MT5 writes datetime strings in the format "2026.03.15 08:07:31" using
dots as date separators. Pydantic expects ISO 8601 format with dashes
like "2026-03-15T08:07:31". Every datetime field in this file has a
field_validator that converts MT5's format to ISO 8601 before Pydantic
attempts to parse it.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ── Enumerations ──────────────────────────────────────────────────────────────

class TradeAction(str, Enum):
    BUY        = "BUY"
    SELL       = "SELL"
    CLOSE      = "CLOSE"
    CLOSE_ALL  = "CLOSE_ALL"
    GET_STATUS = "GET_STATUS"


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


# ── Shared datetime converter ─────────────────────────────────────────────────

def _parse_mt5_dt(v: object) -> object:
    """
    Convert MT5's datetime format to ISO 8601 so Pydantic can parse it.

    MT5 writes:       "2026.03.15 08:07:31"
    Pydantic expects: "2026-03-15T08:07:31"

    The two replacements are:
      1. Replace the first two dots (date separators) with dashes
      2. Replace the space between date and time with T
    """
    if isinstance(v, str):
        return v.replace('.', '-', 2).replace(' ', 'T')
    return v


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
    timestamp:  datetime      = Field(default_factory=datetime.utcnow)

    model_config = {"use_enum_values": True}


# ── Result: MT5 → Python ──────────────────────────────────────────────────────

class TradeResult(BaseModel):
    """
    The outcome written by the MQL5 EA after processing a TradeCommand.
    Python parses this from res_<command_id>.json in the results/ directory.
    """
    command_id:    str
    status:        TradeStatus
    ticket:        Optional[int]      = None
    open_price:    Optional[float]    = None
    close_price:   Optional[float]    = None
    profit:        Optional[float]    = None
    error_code:    Optional[int]      = None
    error_message: Optional[str]      = None
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
    """
    A complete picture of the trading account at a single point in time.
    Returned by the GET_STATUS command.
    """
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