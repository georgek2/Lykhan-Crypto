"""
forex/models.py
───────────────
TradeLog — the single source of truth for every signal and trade outcome.

Design notes
─────────────
- Every signal that enters the system gets a row, even if it is rejected
  by the LLM gate. This gives you a full audit trail for backtesting and
  tuning the LLM prompt over time.
- signal_payload stores the raw TradingView JSON so nothing is lost even
  if the schema changes later.
- signal_indicators is extracted from the payload into its own JSONField
  so you can query e.g. TradeLog.objects.filter(signal_indicators__rsi__lt=30)
  without parsing JSON in Python.
- llm_reasoning stores the model's full explanation, not just approved/rejected.
  This is invaluable for understanding why trades were blocked.
- balance_at_entry / equity_at_entry give you a snapshot of account health
  at the exact moment a trade was placed — critical for post-trade analysis.
- SQLite note: use Celery with --concurrency=1 to avoid database-is-locked
  errors. When migrating to PostgreSQL on AWS RDS, remove that constraint.
"""

from django.db import models


class SignalSource(models.TextChoices):
    TRADINGVIEW = "tradingview", "TradingView"
    MANUAL      = "manual",      "Manual (CLI)"
    LLM         = "llm",         "LLM initiated"
    TEST        = "test",        "Test / Dev"


class TradeStatus(models.TextChoices):
    PENDING  = "PENDING",  "Pending"
    APPROVED = "APPROVED", "LLM approved, awaiting execution"
    EXECUTED = "EXECUTED", "Executed on MT5"
    REJECTED = "REJECTED", "Rejected by LLM gate"
    CLOSED   = "CLOSED",   "Position closed"
    ERROR    = "ERROR",    "Execution error"


class TradeLog(models.Model):
    """
    One row per signal received. Covers the full lifecycle from inbound
    webhook → LLM decision → MT5 execution → position close.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    # command_id links this row to the TradeCommand sent to the FileBridge.
    # It is a UUID string, not a Django auto-increment, so it can be set
    # before the row is saved (useful in the Celery task).
    command_id = models.CharField(
        max_length=36, unique=True, db_index=True,
        help_text="UUID matching the TradeCommand sent to FileBridge"
    )
    # The MT5 ticket number returned after a successful open. Null until
    # the position is actually opened. BigInteger because Deriv uses 64-bit
    # ticket numbers that overflow a regular 32-bit int.
    ticket = models.BigIntegerField(null=True, blank=True)

    # ── Trade parameters ──────────────────────────────────────────────────────
    symbol    = models.CharField(max_length=20, default="EURUSD")
    action    = models.CharField(max_length=10)   # BUY | SELL | CLOSE | CLOSE_ALL
    lot_size  = models.FloatField(default=0.01)
    sl_pips   = models.IntegerField(default=0)
    tp_pips   = models.IntegerField(default=0)
    comment   = models.CharField(max_length=100, blank=True)

    # ── Price and outcome ─────────────────────────────────────────────────────
    entry_price = models.FloatField(null=True, blank=True)
    exit_price  = models.FloatField(null=True, blank=True)
    profit      = models.FloatField(null=True, blank=True)

    # ── Signal provenance ─────────────────────────────────────────────────────
    signal_source = models.CharField(
        max_length=50,
        choices=SignalSource.choices,
        default=SignalSource.TRADINGVIEW,
    )
    # Raw payload exactly as received from TradingView (or the CLI/test caller).
    signal_payload = models.JSONField(
        null=True, blank=True,
        help_text="Raw inbound JSON — never modified after write"
    )
    # Extracted indicator values for querying without JSON parsing.
    # Example: {"rsi": 32.5, "macd": "bullish", "ema_cross": "golden", "timeframe": "H1"}
    signal_indicators = models.JSONField(
        null=True, blank=True,
        help_text="Indicator snapshot extracted from signal_payload"
    )

    # ── LLM gate record ───────────────────────────────────────────────────────
    llm_approved   = models.BooleanField(null=True, help_text="True=approved, False=rejected, None=gate not reached")
    llm_reasoning  = models.TextField(null=True, blank=True, help_text="Full reasoning text from the LLM")
    llm_model_used = models.CharField(max_length=100, null=True, blank=True, help_text="e.g. groq/llama-3.3-70b")
    llm_latency_ms = models.IntegerField(null=True, blank=True, help_text="Time the LLM gate took in milliseconds")
    llm_confidence = models.IntegerField(null=True, blank=True, help_text="0-100 confidence score from the LLM")

    # ── Execution timing ──────────────────────────────────────────────────────
    received_at  = models.DateTimeField(auto_now_add=True, help_text="When the webhook hit Django")
    executed_at  = models.DateTimeField(null=True, blank=True, help_text="When MT5 confirmed the order")
    closed_at    = models.DateTimeField(null=True, blank=True, help_text="When the position was closed")
    execution_latency_ms = models.IntegerField(
        null=True, blank=True,
        help_text="ms from task dispatch to MT5 confirmation"
    )

    # ── Account snapshot at entry ─────────────────────────────────────────────
    balance_at_entry    = models.FloatField(null=True, blank=True)
    equity_at_entry     = models.FloatField(null=True, blank=True)
    free_margin_at_entry = models.FloatField(null=True, blank=True)
    open_trades_at_entry = models.IntegerField(null=True, blank=True)

    # ── Status ────────────────────────────────────────────────────────────────
    status        = models.CharField(max_length=20, choices=TradeStatus.choices, default=TradeStatus.PENDING)
    error_message = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["signal_source"]),
            models.Index(fields=["symbol"]),
            models.Index(fields=["received_at"]),
        ]

    def __str__(self) -> str:
        return f"TradeLog({self.action} {self.symbol} | {self.status} | {self.received_at:%Y-%m-%d %H:%M})"

    # ── Convenience properties ─────────────────────────────────────────────────

    @property
    def is_open(self) -> bool:
        return self.status == TradeStatus.EXECUTED and self.ticket is not None

    @property
    def gross_pnl(self) -> float | None:
        """Realised P&L once the position is closed, else None."""
        if self.profit is not None:
            return round(self.profit, 2)
        return None