"""
forex/services/config/settings.py
──────────────────────────────────
Centralised configuration for the Lykhan Forex service layer.

This file is intentionally separate from Django's own settings module
(project/settings.py) to keep the forex service layer independent of
the web framework. The forex services should remain pure Python — they
know nothing about Django, which makes them portable and testable in
isolation.

All values are loaded from the project's root .env file via
pydantic-settings. If a value is missing from .env, the default
defined here is used instead.
"""

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class ForexSettings(BaseSettings):
    model_config = SettingsConfigDict(
        # Looks for .env in the current working directory, which when
        # running from the lykhan project root resolves correctly.
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # Silently ignore any .env keys we don't define here
    )

    # ── Bridge ───────────────────────────────────────────────────────────────
    # This is the Linux-side path to the folder that MT5's LykhanBridge EA
    # writes its files into. It corresponds to MQL5\Files\mt5bridge\ inside
    # the Bottles Wine environment.
    mt5_bridge_dir: Path = Field(
        default=Path.home() / "mt5bridge",
        description="Linux path to the shared MT5 bridge directory",
    )
    bridge_timeout_seconds: int = Field(
        default=30,
        description="How long Python waits for MT5 to respond before giving up",
    )
    bridge_poll_interval_ms: int = Field(
        default=500,
        description="How frequently Python checks for result files (milliseconds)",
    )

    # ── Trading Defaults ─────────────────────────────────────────────────────
    default_symbol: str   = Field(default="EURUSD")
    default_lot_size: float = Field(default=0.01)
    default_magic_number: int = Field(
        default=20240101,
        description="Magic number tags all trades placed by this agent, "
                    "making them easy to identify and close selectively",
    )
    max_open_trades: int  = Field(default=3)
    default_slippage: int = Field(default=10)

    # ── Risk Management ──────────────────────────────────────────────────────
    default_sl_pips: int = Field(
        default=50,
        description="Stop loss distance in pips. Set to 0 to disable.",
    )
    default_tp_pips: int = Field(
        default=100,
        description="Take profit distance in pips. Set to 0 to disable.",
    )

    # ── Logging ──────────────────────────────────────────────────────────────
    log_level: str  = Field(default="INFO")
    log_file: Path  = Field(default=Path("logs/lykhan_forex.log"))
    
    redis_url: str = Field(
            default="redis://localhost:6379/0",
            description="Redis URL used by SessionBiasCache",
        )
    # ── Monitoring / HFT tuning ─────────────────────────────────────────
    monitor_poll_interval_seconds: int = Field(
        default=1,
        description="Seconds between account snapshot polls in AccountMonitor",
    )
    dashboard_tick_interval: int = Field(
        default=1,
        description="Seconds between dashboard WebSocket account ticks",
    )
    position_watcher_interval_seconds: int = Field(
        default=5,
        description="Recommended interval (seconds) for the lightweight position watcher task",
    )
    # Early-exit / microtrend tuning
    early_exit_virtual_tp_atr_multiplier: float = Field(
        default=2.0,
        description="Multiplier applied to ATR to estimate a virtual TP for positions without explicit TP",
    )
    early_exit_min_profit_amount: float = Field(
        default=0.5,
        description="Minimum floating profit (account currency) before considering early exit for manual positions",
    )
    early_exit_momentum_hold_threshold: float = Field(
        default=0.9,
        description="If microtrend momentum is strong, require this fraction of TP before forcing close",
    )
    # Pullback / peak tracking
    early_exit_pullback_pct: float = Field(
        default=0.3,
        description="Fractional drop from peak profit to trigger an early exit (e.g. 0.3 = 30%)",
    )
    early_exit_peak_ttl_seconds: int = Field(
        default=600,
        description="How long (seconds) to remember the peak P&L for a ticket in Redis",
    )
    early_exit_force_profit_amount: float = Field(
        default=1.0,
        description="Absolute profit (account currency) at or above which the watcher will force-close a position",
    )
    # Force-close retry tuning
    force_close_max_retries: int = Field(
        default=2,
        description="Number of retries when a force-close is rejected before giving up",
    )
    force_close_slippage_increment: int = Field(
        default=10,
        description="Amount to add to slippage (in pips) on each retry attempt",
    )

# Module-level singleton — import this object everywhere in the forex
# service layer instead of instantiating ForexSettings repeatedly.
# Named forex_settings (not settings) to avoid any confusion with
# Django's own `from django.conf import settings`.
forex_settings = ForexSettings()
