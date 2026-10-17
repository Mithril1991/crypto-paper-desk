"""Signals and the BUY / SELL / HOLD rule, with a written reason for every call.

Long-only spot. SELL only ever closes an open paper position; there is no
shorting and no leverage. The rule is a plain trend-following filter chosen to
be readable, not because it is known to work.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from . import indicators as ind
from .config import Settings
from .data import Candle

MIN_1H = 51  # EMA50 needs 50 closes
MIN_4H = 24  # EMA20 plus a 3-bar slope


@dataclass(frozen=True)
class Features:
    price: float
    close_1h: float | None
    ema20_1h: float | None
    ema50_1h: float | None
    rsi14_1h: float | None
    atr_pct_1h: float | None
    vol_pct_1h: float | None
    volume_ratio_1h: float | None
    change_24h_pct: float | None
    close_4h: float | None
    ema20_4h: float | None
    ema20_4h_slope_pct: float | None
    bars_1h: int
    bars_4h: int

    @property
    def trend_4h(self) -> str:
        if self.ema20_4h is None or self.close_4h is None or self.ema20_4h_slope_pct is None:
            return "unknown"
        if self.close_4h > self.ema20_4h and self.ema20_4h_slope_pct > 0:
            return "up"
        if self.close_4h < self.ema20_4h and self.ema20_4h_slope_pct < 0:
            return "down"
        return "flat"

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "trend_4h": self.trend_4h}


def compute_features(price: float, c1: list[Candle], c4: list[Candle]) -> Features:
    closes1 = [c.close for c in c1]
    closes4 = [c.close for c in c4]
    e20 = ind.ema(closes1, 20)
    e50 = ind.ema(closes1, 50)
    e20_4 = ind.ema(closes4, 20)
    slope = None
    if len(e20_4) >= 4:
        slope = 100 * (e20_4[-1] / e20_4[-4] - 1)
    return Features(
        price=price,
        close_1h=closes1[-1] if closes1 else None,
        ema20_1h=e20[-1] if e20 else None,
        ema50_1h=e50[-1] if e50 else None,
        rsi14_1h=ind.rsi(closes1),
        atr_pct_1h=ind.atr_pct(c1),
        vol_pct_1h=ind.realized_vol_pct(closes1),
        volume_ratio_1h=ind.volume_ratio(c1),
        change_24h_pct=ind.pct_change(closes1, 24),
        close_4h=closes4[-1] if closes4 else None,
        ema20_4h=e20_4[-1] if e20_4 else None,
        ema20_4h_slope_pct=slope,
        bars_1h=len(c1),
        bars_4h=len(c4),
    )


@dataclass(frozen=True)
class OpenPosition:
    entry_price: float
    stop_price: float
    take_price: float


@dataclass(frozen=True)
class Decision:
    action: str  # BUY | SELL | HOLD
    reason: str
    exit_kind: str | None = None  # stop | take | signal, for SELL


def _f(x: float | None, digits: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def describe(f: Features, s: Settings) -> str:
    notes = [
        f"4h trend {f.trend_4h} (close {_f(f.close_4h, 4)} vs EMA20 {_f(f.ema20_4h, 4)}, slope {_f(f.ema20_4h_slope_pct)}%)",
        f"1h EMA20/50 {_f(f.ema20_1h, 4)}/{_f(f.ema50_1h, 4)}",
        f"RSI14 {_f(f.rsi14_1h, 1)}",
        f"ATR {_f(f.atr_pct_1h)}%/h",
        f"24h {_f(f.change_24h_pct)}%",
    ]
    if f.volume_ratio_1h is not None and f.volume_ratio_1h >= s.volume_spike_ratio:
        notes.append(f"VOLUME SPIKE {f.volume_ratio_1h:.1f}x median")
    return "; ".join(notes)


def decide(f: Features, position: OpenPosition | None, s: Settings) -> Decision:
    context = describe(f, s)
    if position is not None:
        if f.price <= position.stop_price:
            return Decision("SELL", f"Stop-loss: price {f.price:.6g} <= stop {position.stop_price:.6g}. {context}", "stop")
        if f.price >= position.take_price:
            return Decision("SELL", f"Take-profit: price {f.price:.6g} >= target {position.take_price:.6g}. {context}", "take")
        broken_1h = f.ema50_1h is not None and f.close_1h is not None and f.close_1h < f.ema50_1h
        if f.trend_4h == "down" and broken_1h:
            return Decision("SELL", f"Trend exit: 4h trend turned down and 1h close is below EMA50. {context}", "signal")
        return Decision("HOLD", f"Holding: no exit rule hit. {context}")

    if f.bars_1h < MIN_1H or f.bars_4h < MIN_4H:
        return Decision("HOLD", f"Warming up: {f.bars_1h}/{MIN_1H} 1h and {f.bars_4h}/{MIN_4H} 4h closed candles. {context}")
    failed = []
    if f.trend_4h != "up":
        failed.append(f"4h trend is {f.trend_4h}, need up")
    if not (f.ema20_1h and f.ema50_1h and f.ema20_1h > f.ema50_1h):
        failed.append("1h EMA20 not above EMA50")
    if f.rsi14_1h is None or not (s.rsi_entry_min <= f.rsi14_1h <= s.rsi_entry_max):
        failed.append(f"RSI {_f(f.rsi14_1h, 1)} outside {s.rsi_entry_min:g}-{s.rsi_entry_max:g}")
    if f.atr_pct_1h is None or f.atr_pct_1h > s.max_atr_pct_1h:
        failed.append(f"1h ATR {_f(f.atr_pct_1h)}% above {s.max_atr_pct_1h:g}% (a {s.stop_loss_pct:.0%} stop would sit inside the noise)")
    if failed:
        return Decision("HOLD", f"No entry: {'; '.join(failed)}. {context}")
    return Decision("BUY", f"Entry: 4h uptrend, 1h EMA20 > EMA50, RSI in range, volatility acceptable. {context}")
