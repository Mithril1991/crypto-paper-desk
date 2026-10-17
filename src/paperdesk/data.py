"""Market data, read only through the dexpaprika and ccxt MCP servers.

Nothing here talks to an exchange or a chain directly, holds a key, or can
place an order: the two servers expose read-only tools and that is all we call.
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .config import Asset, ServerSpec

RATIONALE = "Paper-trading research agent; reading market data to compute signals for a simulated ledger."


class DataError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candle:
    ts: int  # open time, unix seconds UTC
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Quote:
    last: float
    bid: float | None
    ask: float | None
    volume_24h_usd: float | None


@dataclass(frozen=True)
class ToolResult:
    text: str
    structured: dict[str, Any] | None


class ToolCaller(Protocol):
    async def call(self, server: str, tool: str, args: dict[str, Any]) -> ToolResult: ...


class McpToolCaller:
    """Opens each configured MCP server over stdio once per cycle, on first use."""

    def __init__(self, servers: dict[str, ServerSpec]) -> None:
        self._servers = servers
        self._sessions: dict[str, Any] = {}
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> McpToolCaller:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    async def _session(self, server: str) -> Any:
        if server in self._sessions:
            return self._sessions[server]
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        spec = self._servers[server]
        params = StdioServerParameters(
            command=spec.command, args=list(spec.args), env={**os.environ, **dict(spec.env)}
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._sessions[server] = session
        return session

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> ToolResult:
        session = await self._session(server)
        res = await session.call_tool(tool, args)
        # mcp 1.x uses camelCase attributes, 2.x snake_case.
        is_error = getattr(res, "is_error", None) or getattr(res, "isError", False)
        structured = getattr(res, "structured_content", None) or getattr(res, "structuredContent", None)
        text = "\n".join(getattr(c, "text", "") for c in res.content)
        if is_error:
            raise DataError(f"{server}.{tool}: {text[:300]}")
        return ToolResult(text=text, structured=structured)


# ── ccxt server: it answers in formatted text, so parse that text ──────────────

def _num(value: str) -> float | None:
    value = value.strip()
    if value in ("", "N/A", "None"):
        return None
    return float(value)


def _check_ccxt(text: str) -> None:
    if text.startswith("Error"):
        raise DataError(text[:300])


def parse_ccxt_summary(text: str) -> Quote:
    _check_ccxt(text)
    found = dict(re.findall(r"^(Last Price|Bid|Ask|24h Volume): (.*)$", text, re.MULTILINE))
    last = _num(found.get("Last Price", ""))
    if last is None:
        raise DataError(f"no last price in ccxt summary: {text[:200]}")
    base_volume = _num(found.get("24h Volume", ""))
    return Quote(
        last=last,
        bid=_num(found.get("Bid", "")),
        ask=_num(found.get("Ask", "")),
        volume_24h_usd=base_volume * last if base_volume is not None else None,
    )


def parse_ccxt_ohlcv(text: str) -> list[Candle]:
    _check_ccxt(text)
    candles = []
    for block in text.split("---"):
        fields = dict(re.findall(r"^(Time|Open|High|Low|Close|Volume): (.*)$", block, re.MULTILINE))
        if "Time" not in fields:
            continue
        # The server prints local wall time of the machine it runs on, which is this machine.
        ts = int(datetime.strptime(fields["Time"], "%Y-%m-%d %H:%M:%S").timestamp())
        candles.append(Candle(ts, float(fields["Open"]), float(fields["High"]),
                              float(fields["Low"]), float(fields["Close"]), float(fields["Volume"])))
    return candles


# ── dexpaprika server: JSON, preferably as structuredContent ──────────────────

def _payload(result: ToolResult) -> dict[str, Any]:
    if result.structured:
        return result.structured
    try:
        return json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise DataError(f"dexpaprika returned non-JSON: {result.text[:200]}") from exc


def parse_dex_token(payload: dict[str, Any]) -> tuple[float | None, float | None, float | None]:
    """Return (price_usd, liquidity_usd, volume_24h_usd)."""
    summary = payload.get("summary") or payload
    day = summary.get("24h") or {}
    return summary.get("price_usd"), summary.get("liquidity_usd"), day.get("volume_usd")


def parse_dex_top_pool(payload: dict[str, Any]) -> str | None:
    rows = payload.get("results") or payload.get("pools") or []
    for row in rows:
        address = row.get("id") or row.get("address")
        if address:
            return address
    return None


def parse_dex_ohlcv(payload: dict[str, Any]) -> list[Candle]:
    rows = payload.get("ohlcv", payload if isinstance(payload, list) else [])
    candles = []
    for row in rows:
        ts = int(datetime.fromisoformat(row["time_open"].replace("Z", "+00:00")).timestamp())
        candles.append(Candle(ts, float(row["open"]), float(row["high"]), float(row["low"]),
                              float(row["close"]), float(row.get("volume") or 0)))
    return candles


class Cache(Protocol):
    def cache_get(self, key: str, max_age_s: float) -> Any | None: ...
    def cache_put(self, key: str, value: Any) -> None: ...


@dataclass(frozen=True)
class Snapshot:
    """Everything one cycle knows about one asset."""

    quote: Quote
    candles_1h: list[Candle]
    candles_4h: list[Candle]
    liquidity_usd: float | None
    liquidity_source: str
    venue: str  # "cex" or "dex": decides slippage


class MarketData:
    def __init__(self, caller: ToolCaller, cache: Cache, dex_cache_minutes: int,
                 dex_pool_cache_hours: int, now: float | None = None) -> None:
        self._caller = caller
        self._cache = cache
        self._dex_ttl = dex_cache_minutes * 60
        self._pool_ttl = dex_pool_cache_hours * 3600
        self._now = now if now is not None else time.time()

    async def snapshot(self, asset: Asset) -> Snapshot:
        if asset.cex is not None:
            return await self._cex_snapshot(asset)
        return await self._dex_snapshot(asset)

    async def _cex_snapshot(self, asset: Asset) -> Snapshot:
        cex = asset.cex
        assert cex is not None
        base = {"symbol": cex.pair, "exchange": cex.exchange}
        quote = parse_ccxt_summary((await self._caller.call("ccxt", "get-market-summary", base)).text)
        c1 = parse_ccxt_ohlcv((await self._caller.call(
            "ccxt", "get-historical-ohlcv", {**base, "timeframe": "1h", "days_back": 7})).text)
        c4 = parse_ccxt_ohlcv((await self._caller.call(
            "ccxt", "get-historical-ohlcv", {**base, "timeframe": "4h", "days_back": 14})).text)
        if asset.dex is not None:
            _, liquidity, _ = await self._dex_token(asset, fresh=False)
            source = "dexpaprika liquidity_usd across pools"
        else:
            liquidity = quote.volume_24h_usd
            source = f"{cex.exchange} 24h volume (proxy: no DEX mapping)"
        return Snapshot(quote, completed(c1, 3600, self._now), completed(c4, 4 * 3600, self._now),
                        liquidity, source, "cex")

    async def _dex_snapshot(self, asset: Asset) -> Snapshot:
        price, liquidity, volume = await self._dex_token(asset, fresh=True)
        if price is None:
            raise DataError(f"{asset.symbol}: dexpaprika has no USD price")
        pool = await self._dex_pool(asset)
        key = f"ohlcv:{asset.dex.network}:{pool}"  # type: ignore[union-attr]
        rows = self._cache.cache_get(key, self._dex_ttl)
        if rows is None:
            # Keyless DexPaprika serves the last 24h at 1h; history beyond that accumulates in the ledger.
            payload = _payload(await self._caller.call("dexpaprika", "getPoolOHLCV", {
                "network": asset.dex.network, "pool_address": pool,  # type: ignore[union-attr]
                "start": "-24h", "interval": "1h", "limit": 24, "rationale": RATIONALE}))
            rows = payload.get("ohlcv", [])
            self._cache.cache_put(key, rows)
        c1 = completed(parse_dex_ohlcv({"ohlcv": rows}), 3600, self._now)
        quote = Quote(last=price, bid=None, ask=None, volume_24h_usd=volume)
        return Snapshot(quote, c1, [], liquidity, "dexpaprika liquidity_usd across pools", "dex")

    async def _dex_token(self, asset: Asset, fresh: bool) -> tuple[float | None, float | None, float | None]:
        dex = asset.dex
        assert dex is not None
        key = f"token:{dex.network}:{dex.token_address}"
        cached = None if fresh else self._cache.cache_get(key, self._dex_ttl)
        if cached is None:
            payload = _payload(await self._caller.call("dexpaprika", "getTokenDetails", {
                "network": dex.network, "token_address": dex.token_address, "rationale": RATIONALE}))
            cached = list(parse_dex_token(payload))
            self._cache.cache_put(key, cached)
        return cached[0], cached[1], cached[2]

    async def _dex_pool(self, asset: Asset) -> str:
        dex = asset.dex
        assert dex is not None
        if dex.pool_address:
            return dex.pool_address
        key = f"pool:{dex.network}:{dex.token_address}"
        pool = self._cache.cache_get(key, self._pool_ttl)
        if pool is None:
            payload = _payload(await self._caller.call("dexpaprika", "getTokenPools", {
                "network": dex.network, "token_address": dex.token_address,
                "limit": 5, "rationale": RATIONALE}))
            pool = parse_dex_top_pool(payload)
            if pool is None:
                raise DataError(f"{asset.symbol}: no pools found on {dex.network}")
            self._cache.cache_put(key, pool)
        return pool


def completed(candles: list[Candle], seconds: int, now: float) -> list[Candle]:
    """Drop the still-forming candle; signals use closed candles only."""
    return sorted((c for c in candles if c.ts + seconds <= now), key=lambda c: c.ts)


def resample(candles: list[Candle], seconds: int) -> list[Candle]:
    """Build complete coarser candles (e.g. 4h from 1h); partial buckets are dropped."""
    if not candles:
        return []
    step = candles[1].ts - candles[0].ts if len(candles) > 1 else 3600
    per = seconds // step
    buckets: dict[int, list[Candle]] = {}
    for c in candles:
        buckets.setdefault(c.ts - c.ts % seconds, []).append(c)
    out = []
    for start in sorted(buckets):
        group = sorted(buckets[start], key=lambda c: c.ts)
        if len(group) < per:
            continue
        out.append(Candle(start, group[0].open, max(c.high for c in group),
                          min(c.low for c in group), group[-1].close, sum(c.volume for c in group)))
    return out
