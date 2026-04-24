"""forex/services/market/microtrend.py
Micro-trend analyzer for 5-10min scalping signals.
"""

from .ohlcv import rsi, ema, last, stochastic_kd, bollinger_bands, volume_proxy
from forex.services.core.schemas import CandleData
from typing import Dict


def analyze_micro(candle_data: CandleData) -> Dict[str, float | str]:
    closes = candle_data.closes
    highs = candle_data.highs
    lows = candle_data.lows

    # RSI(5)
    rsi5_list = rsi(closes, 5)
    rsi5 = last(rsi5_list) or 50

    # Stochastic(5,3,3)
    stoch = stochastic_kd(closes, highs, lows, 5, 3, 3)
    stoch_k = last(stoch["k"])
    stoch_d = last(stoch["d"])

    # Velocity 5
    vel5 = 0.0
    if len(closes) >= 6:
        vel5 = (closes[-1] - closes[-6]) / 5

    # EMA5/10
    ema5 = last(ema(closes, 5))
    ema10 = last(ema(closes, 10))
    ema_align = 1 if ema5 and ema10 and ema5 > ema10 else -1 if ema5 and ema10 and ema5 < ema10 else 0

    # Bollinger Bands (20,2)
    bb = bollinger_bands(closes, 20, 2.0)
    bb_width = last(bb["width"])

    # Volume proxy (avg body size last 5 bars)
    vol_proxy = volume_proxy(candle_data, 5)

    # Microtrend score (expanded)
    score = 0
    if rsi5 < 30:
        score += 1
    elif rsi5 > 70:
        score -= 1
    if stoch_k and stoch_d:
        if stoch_k > stoch_d and stoch_k < 30:
            score += 1
        elif stoch_k < stoch_d and stoch_k > 70:
            score -= 1
    if vel5 > 0:
        score += ema_align * 0.5
    elif vel5 < 0:
        score -= ema_align * 0.5
    if bb_width and bb_width < 0.01:
        score += 0.5  # BB squeeze, expect breakout
    if vol_proxy and vol_proxy > 0.0001:
        score += 0.5  # High recent volatility

    micro_bias = "LONG" if score > 0 else "SHORT" if score < 0 else "NEUTRAL"
    return {
        "micro_bias": micro_bias,
        "score": round(score, 2),
        "rsi5": round(rsi5, 1),
        "velocity_5": vel5,
        "ema_align": ema_align,
        "stoch_k": stoch_k,
        "stoch_d": stoch_d,
        "bb_width": bb_width,
        "volume_proxy": vol_proxy,
    }
