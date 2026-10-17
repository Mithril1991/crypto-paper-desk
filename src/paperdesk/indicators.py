"""Plain-python indicators over closed candles."""

from __future__ import annotations

import statistics

from .data import Candle


def ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    gains, losses = [], []
    for a, b in zip(closes, closes[1:]):
        gains.append(max(b - a, 0.0))
        losses.append(max(a - b, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def atr_pct(candles: list[Candle], period: int = 14) -> float | None:
    """Wilder ATR as a percent of the last close."""
    if len(candles) <= period:
        return None
    trs = [max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
           for p, c in zip(candles, candles[1:])]
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return 100 * atr / candles[-1].close


def realized_vol_pct(closes: list[float], window: int = 24) -> float | None:
    """Stdev of the last `window` simple returns, in percent per bar."""
    if len(closes) <= window:
        return None
    tail = closes[-window - 1:]
    rets = [b / a - 1 for a, b in zip(tail, tail[1:])]
    return 100 * statistics.stdev(rets)


def volume_ratio(candles: list[Candle], window: int = 24) -> float | None:
    """Last closed bar's volume over the median of the `window` bars before it."""
    if len(candles) <= window:
        return None
    base = statistics.median(c.volume for c in candles[-window - 1:-1])
    if base <= 0:
        return None
    return candles[-1].volume / base


def pct_change(closes: list[float], bars: int) -> float | None:
    if len(closes) <= bars:
        return None
    return 100 * (closes[-1] / closes[-1 - bars] - 1)
