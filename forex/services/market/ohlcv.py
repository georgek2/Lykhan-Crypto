from __future__ import annotations

"""
forex/services/market/ohlcv.py
───────────────────────────────
Fetches multi-timeframe OHLCV candle data from MT5 via the FileBridge.

The OHLCVFetcher is the data layer for the strategic analysis loop.
Every 30 minutes, the Celery Beat task calls fetch_multi_timeframe()
to get candles for M5, H1, D1, and W1. These are passed to ChartAnalyzer
which sends them to the LLM for session bias determination.

The HFT scanner also calls fetch_candles("EURUSD", "M1", 50) directly
when it needs fresh M1 data for its indicator calculations.

Pure indicator calculations (EMA, RSI, MACD, ATR) are implemented here
in pure Python with no external dependencies — no TA-Lib, no pandas.
These run on the raw price arrays returned from CandleData.
"""

import logging
from typing import Optional

from forex.services.bridge.file_bridge import FileBridge, BridgeError
from forex.services.core.schemas import CandleData, Timeframe

logger = logging.getLogger(__name__)

# Timeframes fetched for the strategic LLM analysis each session
STRATEGIC_TIMEFRAMES = [
    Timeframe.M5,
    Timeframe.H1,
    Timeframe.D1,
    Timeframe.W1,
]

# Candle counts per timeframe for the strategic analysis
# M5: last 4 hours, H1: last 5 days, D1: last 3 months, W1: last year
STRATEGIC_COUNTS = {
    Timeframe.M5:  48,
    Timeframe.H1:  120,
    Timeframe.D1:  90,
    Timeframe.W1:  52,
}


class OHLCVFetcher:
    """
    Fetches OHLCV candle data from MT5 via the FileBridge.

    Usage:
        fetcher = OHLCVFetcher()
        candles = fetcher.fetch_candles("EURUSD", "M1", count=50)
        multi   = fetcher.fetch_multi_timeframe("EURUSD")
    """

    def __init__(self, bridge: Optional[FileBridge] = None) -> None:
        self._bridge = bridge or FileBridge()

    def fetch_candles(
        self,
        symbol:    str,
        timeframe: str,
        count:     int = 100,
    ) -> CandleData | None:
        """
        Fetch a single series of candles. Returns None if the bridge fails
        rather than raising, so callers can handle gracefully.
        """
        try:
            data = self._bridge.get_candles(symbol, timeframe, count)
            logger.debug(
                "OHLCVFetcher: %s %s — fetched %d bars",
                symbol, timeframe, len(data.bars),
            )
            return data
        except BridgeError as exc:
            logger.error("OHLCVFetcher: bridge error for %s %s — %s", symbol, timeframe, exc)
            return None
        except Exception as exc:
            logger.exception("OHLCVFetcher: unexpected error for %s %s", symbol, timeframe)
            return None

    def fetch_multi_timeframe(
        self,
        symbol: str,
        timeframes: list[str] | None = None,
    ) -> dict[str, CandleData]:
        """
        Fetch candles for multiple timeframes in sequence.
        Returns only the timeframes that succeeded — partial results are
        better than failing completely when one timeframe times out.

        :param symbol: e.g. "EURUSD"
        :param timeframes: list of timeframe strings; defaults to STRATEGIC_TIMEFRAMES
        :returns: dict mapping timeframe string to CandleData
        """
        tfs     = timeframes or [tf.value for tf in STRATEGIC_TIMEFRAMES]
        results = {}

        for tf in tfs:
            count = STRATEGIC_COUNTS.get(tf, 100)
            data  = self.fetch_candles(symbol, tf, count)
            if data is not None:
                results[tf] = data
            else:
                logger.warning("OHLCVFetcher: skipped %s %s (fetch failed)", symbol, tf)

        logger.info(
            "OHLCVFetcher: fetched %d/%d timeframes for %s",
            len(results), len(tfs), symbol,
        )
        return results


# ── Pure Python indicator calculations ───────────────────────────────────────
# These take plain Python lists of floats (close prices, highs, lows).
# No external dependencies — works on any environment including AWS Lambda.

def last(series):
    vals = [v for v in series if v is not None]
    return round(vals[-1], 5) if vals else None

def last2(series):
    vals = [v for v in series if v is not None]
    return round(vals[-2], 5) if len(vals) >= 2 else None

def ema(prices: list[float], period: int) -> list[float]:
    """
    Exponential Moving Average.
    Returns a list of the same length as prices; first (period-1) values are None.
    """
    if len(prices) < period:
        return [None] * len(prices)

    k      = 2.0 / (period + 1)
    result = [None] * (period - 1)
    seed   = sum(prices[:period]) / period
    result.append(seed)

    for price in prices[period:]:
        result.append(price * k + result[-1] * (1 - k))

    return result


def rsi(prices: list[float], period: int = 14) -> list[float | None]:
    """
    Wilder's RSI. Returns values 0-100; first period values are None.
    """
    if len(prices) < period + 1:
        return [None] * len(prices)

    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains  = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    result = [None] * period

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            result.append(100.0)
        else:
            rs = avg_gain / avg_loss
            result.append(round(100 - (100 / (1 + rs)), 2))

    return result


def macd(
    prices:       list[float],
    fast_period:  int = 12,
    slow_period:  int = 26,
    signal_period: int = 9,
) -> dict[str, list[float | None]]:
    """
    MACD indicator.
    Returns dict with keys: 'macd', 'signal', 'histogram'
    All lists are the same length as prices; early values are None.
    """
    fast_ema   = ema(prices, fast_period)
    slow_ema   = ema(prices, slow_period)

    macd_line  = [
        (f - s) if f is not None and s is not None else None
        for f, s in zip(fast_ema, slow_ema)
    ]

    valid_macd = [v for v in macd_line if v is not None]
    signal_raw = ema(valid_macd, signal_period)
    offset     = len(macd_line) - len(signal_raw)
    signal_line = [None] * offset + signal_raw

    histogram = [
        (m - s) if m is not None and s is not None else None
        for m, s in zip(macd_line, signal_line)
    ]

    return {
        "macd":      macd_line,
        "signal":    signal_line,
        "histogram": histogram,
    }


def atr(
    highs:  list[float],
    lows:   list[float],
    closes: list[float],
    period: int = 14,
) -> list[float | None]:
    """
    Average True Range. Returns ATR values; first period values are None.
    """
    if len(closes) < 2:
        return [None] * len(closes)

    true_ranges = [None]
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)

    result = [None] * period
    seed   = sum(tr for tr in true_ranges[1:period + 1] if tr is not None) / period
    result.append(seed)

    for tr in true_ranges[period + 1:]:
        result.append((result[-1] * (period - 1) + tr) / period)

    return result


def stochastic_kd(prices: list[float], highs: list[float], lows: list[float], k_period: int = 5, d_period: int = 3, smooth_k: int = 3) -> dict:
    """
    Stochastic Oscillator (%K, %D).
    Returns dict with 'k' and 'd' lists.
    """
    if len(prices) < k_period:
        return {"k": [None]*len(prices), "d": [None]*len(prices)}
    k_values = []
    for i in range(len(prices)):
        if i < k_period - 1:
            k_values.append(None)
        else:
            low_min = min(lows[i-k_period+1:i+1])
            high_max = max(highs[i-k_period+1:i+1])
            if high_max - low_min == 0:
                k_values.append(0)
            else:
                k_values.append(100 * (prices[i] - low_min) / (high_max - low_min))
    # Smooth %K
    k_smooth = ema([v for v in k_values if v is not None], smooth_k)
    k_smooth = [None]*(len(k_values)-len(k_smooth)) + k_smooth
    # %D is SMA of %K
    d_values = []
    for i in range(len(k_smooth)):
        if i < d_period-1 or k_smooth[i] is None:
            d_values.append(None)
        else:
            window = [k for k in k_smooth[i-d_period+1:i+1] if k is not None]
            d_values.append(sum(window)/len(window) if window else None)
    return {"k": k_smooth, "d": d_values}

def bollinger_bands(prices: list[float], period: int = 20, num_std: float = 2.0) -> dict:
    """
    Bollinger Bands and width.
    Returns dict with 'upper', 'lower', 'middle', 'width' lists.
    """
    if len(prices) < period:
        n = len(prices)
        return {"upper": [None]*n, "lower": [None]*n, "middle": [None]*n, "width": [None]*n}
    middle = []
    upper = []
    lower = []
    width = []
    for i in range(len(prices)):
        if i < period-1:
            middle.append(None)
            upper.append(None)
            lower.append(None)
            width.append(None)
        else:
            window = prices[i-period+1:i+1]
            m = sum(window)/period
            std = (sum((x-m)**2 for x in window)/period)**0.5
            middle.append(m)
            upper.append(m + num_std*std)
            lower.append(m - num_std*std)
            width.append((upper[-1] - lower[-1])/m if m != 0 else None)
    return {"upper": upper, "lower": lower, "middle": middle, "width": width}

def volume_proxy(data: CandleData, period: int = 5) -> float:
    """
    Simple volume proxy: average candle body size over period.
    """
    if len(data.bars) < period:
        return 0.0
    bodies = [abs(bar.close - bar.open) for bar in data.bars[-period:]]
    return sum(bodies)/len(bodies) if bodies else 0.0


def compute_indicators(data: CandleData) -> dict:
    """
    Compute the full indicator set for a CandleData object.
    Returns a dict of the most recent values — ready to embed in the
    LLM prompt or pass to the HFT scanner's entry logic.
    """
    closes = data.closes
    highs  = data.highs
    lows   = data.lows

    ema9_series  = ema(closes, 9)
    ema21_series = ema(closes, 21)
    rsi_series   = rsi(closes, 14)
    macd_data    = macd(closes)
    atr_series   = atr(highs, lows, closes, 14)
    stoch_data   = stochastic_kd(closes, highs, lows, 5, 3, 3)
    bb_data      = bollinger_bands(closes, 20, 2.0)
    vol_proxy    = volume_proxy(data, 5)

    ema9_now  = last(ema9_series)
    ema21_now = last(ema21_series)
    ema9_prev = last2(ema9_series)
    ema21_prev = last2(ema21_series)

    # Detect EMA crossover
    ema_cross = "none"
    if all(v is not None for v in [ema9_now, ema21_now, ema9_prev, ema21_prev]):
        if ema9_prev <= ema21_prev and ema9_now > ema21_now:
            ema_cross = "golden"   # bullish crossover
        elif ema9_prev >= ema21_prev and ema9_now < ema21_now:
            ema_cross = "death"    # bearish crossover

    macd_hist_now  = last(macd_data["histogram"])
    macd_hist_prev = last2(macd_data["histogram"])
    macd_direction = "neutral"
    if macd_hist_now is not None and macd_hist_prev is not None:
        if macd_hist_now > 0 and macd_hist_now > macd_hist_prev:
            macd_direction = "bullish_increasing"
        elif macd_hist_now > 0:
            macd_direction = "bullish_weakening"
        elif macd_hist_now < 0 and macd_hist_now < macd_hist_prev:
            macd_direction = "bearish_increasing"
        elif macd_hist_now < 0:
            macd_direction = "bearish_weakening"

    atr_now = last(atr_series)
    stoch_k = last(stoch_data["k"])
    stoch_d = last(stoch_data["d"])
    bb_upper = last(bb_data["upper"])
    bb_lower = last(bb_data["lower"])
    bb_middle = last(bb_data["middle"])
    bb_width = last(bb_data["width"])

    # Recommended ATR-based SL/TP
    atr_sl_pips = round(atr_now * 1.5 * 10000) if atr_now else None
    atr_tp_pips = round(atr_now * 2.0 * 10000) if atr_now else None

    latest = data.latest
    return {
        "timeframe":     data.timeframe,
        "bar_count":     len(closes),
        "last_close":    round(latest.close, 5) if latest else None,
        "ema9":          ema9_now,
        "ema21":         ema21_now,
        "ema_cross":     ema_cross,
        "rsi":           last(rsi_series),
        "macd_line":     last(macd_data["macd"]),
        "macd_histogram": macd_hist_now,
        "macd_direction": macd_direction,
        "atr":           atr_now,
        "atr_sl_pips":   atr_sl_pips,
        "atr_tp_pips":   atr_tp_pips,
        "stoch_k":       stoch_k,
        "stoch_d":       stoch_d,
        "bb_upper":      bb_upper,
        "bb_lower":      bb_lower,
        "bb_middle":     bb_middle,
        "bb_width":      bb_width,
        "volume_proxy":  vol_proxy,
    }

