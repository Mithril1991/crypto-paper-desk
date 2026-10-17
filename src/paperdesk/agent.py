"""One research cycle: read data, compute signals, decide, apply paper fills."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from .config import Asset, Paths, Settings
from .data import MarketData, Snapshot, ToolCaller, resample
from .ledger import Ledger
from .strategy import Decision, OpenPosition, compute_features, decide

log = logging.getLogger("paperdesk")

WEEK = 7 * 24 * 3600


@dataclass(frozen=True)
class Outcome:
    symbol: str
    action: str
    executed: bool
    reason: str
    blocked_by: str | None = None


def fill_price(ref: float, side: str, venue: str, s: Settings) -> float:
    bps = s.dex_slippage_bps if venue == "dex" else s.cex_slippage_bps
    sign = 1 if side == "BUY" else -1
    return ref * (1 + sign * bps / 10_000)


async def run_cycle(paths: Paths, settings: Settings, assets: list[Asset], caller: ToolCaller,
                    ledger: Ledger, now: float) -> list[Outcome]:
    market = MarketData(caller, ledger, settings.dex_cache_minutes, settings.dex_pool_cache_hours, now=now)
    snaps: dict[str, Snapshot] = {}
    for asset in assets:
        try:
            snap = await market.snapshot(asset)
        except Exception as exc:  # one bad asset must not stop the cycle
            log.warning("%s: data error: %s", asset.symbol, exc)
            ledger.record_error(asset.symbol, f"{type(exc).__name__}: {exc}")
            continue
        if asset.dex_only:
            ledger.upsert_candles(asset.symbol, snap.candles_1h)
            history = ledger.candles(asset.symbol, now - WEEK)
            snap = Snapshot(snap.quote, history, resample(history, 4 * 3600), snap.liquidity_usd,
                            snap.liquidity_source, snap.venue)
        snaps[asset.symbol] = snap

    marks = {sym: snap.quote.last for sym, snap in snaps.items()}
    equity = ledger.cash() + sum(p.qty * marks.get(p.symbol, p.entry_price) for p in ledger.open_positions())

    outcomes = []
    # Exits before entries, so a closed position frees its slot within the same cycle.
    ordered = sorted(snaps.items(), key=lambda kv: ledger.open_position(kv[0]) is None)
    for symbol, snap in ordered:
        outcomes.append(_act(symbol, snap, settings, ledger, equity, paths))

    ledger.record_equity(marks)
    return outcomes


def _act(symbol: str, snap: Snapshot, s: Settings, ledger: Ledger, equity: float, paths: Paths) -> Outcome:
    pos = ledger.open_position(symbol)
    features = compute_features(snap.quote.last, snap.candles_1h, snap.candles_4h)
    fdict: dict[str, Any] = {**features.as_dict(), "liquidity_usd": snap.liquidity_usd,
                             "liquidity_source": snap.liquidity_source, "venue": snap.venue}

    if pos is None and (snap.liquidity_usd is None or snap.liquidity_usd < s.min_liquidity_usd):
        liq = "unknown" if snap.liquidity_usd is None else f"${snap.liquidity_usd:,.0f}"
        out = Outcome(symbol, "SKIP", False,
                      f"Skipped: liquidity {liq} ({snap.liquidity_source}) below ${s.min_liquidity_usd:,.0f}.")
        return _journal(out, snap, fdict, ledger, paths)

    open_pos = OpenPosition(pos.entry_price, pos.stop_price, pos.take_price) if pos else None
    decision: Decision = decide(features, open_pos, s)

    if decision.action == "SELL" and pos is not None:
        ref = snap.quote.bid or snap.quote.last
        price = fill_price(ref, "SELL", snap.venue, s)
        fee = pos.qty * price * s.fee_bps / 10_000
        pnl = ledger.record_exit(pos, ref, price, fee, decision.reason, decision.exit_kind or "signal")
        out = Outcome(symbol, "SELL", True, f"{decision.reason} Paper P&L ${pnl:,.2f}.")
        return _journal(out, snap, fdict, ledger, paths)

    if decision.action == "BUY":
        blocked = None
        if len(ledger.open_positions()) >= s.max_open_positions:
            blocked = f"max {s.max_open_positions} open positions"
        budget = min(equity * s.max_position_pct, ledger.cash())
        if blocked is None and budget < 10:
            blocked = "not enough paper cash"
        if blocked:
            out = Outcome(symbol, "BUY", False, decision.reason, blocked)
            return _journal(out, snap, fdict, ledger, paths)
        ref = snap.quote.ask or snap.quote.last
        price = fill_price(ref, "BUY", snap.venue, s)
        # Size so notional plus fee stays within the per-position cap.
        notional = budget / (1 + s.fee_bps / 10_000)
        qty = notional / price
        fee = notional * s.fee_bps / 10_000
        ledger.record_entry(symbol, snap.venue, qty, ref, price, fee,
                            stop=price * (1 - s.stop_loss_pct), take=price * (1 + s.take_profit_pct),
                            reason=decision.reason)
        out = Outcome(symbol, "BUY", True, f"{decision.reason} Paper buy ${notional:,.2f} at {price:.6g}.")
        return _journal(out, snap, fdict, ledger, paths)

    return _journal(Outcome(symbol, "HOLD", False, decision.reason), snap, fdict, ledger, paths)


def _journal(out: Outcome, snap: Snapshot, features: dict[str, Any], ledger: Ledger, paths: Paths) -> Outcome:
    ledger.record_decision(out.symbol, snap.quote.last, out.action, out.executed, out.reason, out.blocked_by, features)
    paths.reasoning_log.parent.mkdir(parents=True, exist_ok=True)
    with paths.reasoning_log.open("a") as fh:
        fh.write(json.dumps({"ts": ledger.now(), "symbol": out.symbol, "price": snap.quote.last,
                             "action": out.action, "executed": out.executed, "blocked_by": out.blocked_by,
                             "reason": out.reason}) + "\n")
    tag = "" if out.executed or out.action in ("HOLD", "SKIP") else f" [blocked: {out.blocked_by}]"
    log.info("%-6s %-4s%s %s", out.symbol, out.action, tag, out.reason)
    return out
