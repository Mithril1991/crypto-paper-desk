"""A stand-in for the two MCP servers that answers in their exact output formats."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from paperdesk.data import ToolResult

# Every tool the agent may call. Both servers are read-only; nothing here can trade.
READ_ONLY_TOOLS = {
    ("ccxt", "get-market-summary"),
    ("ccxt", "get-historical-ohlcv"),
    ("dexpaprika", "getTokenDetails"),
    ("dexpaprika", "getTokenPools"),
    ("dexpaprika", "getPoolOHLCV"),
}


def path(n: int, start: float, drift: float, wiggle: float = 0.004) -> list[float]:
    return [start * (1 + drift) ** i * (1 + wiggle * math.sin(i)) for i in range(n)]


def ccxt_ohlcv_text(symbol: str, exchange: str, timeframe: str, start_ts: int, step: int,
                    closes: list[float]) -> str:
    """Mirrors format_ohlcv_data in Nayshins/mcp-server-ccxt src/server.py."""
    blocks = []
    for i, close in enumerate(closes):
        open_ = closes[i - 1] if i else close
        high, low = max(open_, close) * 1.002, min(open_, close) * 0.998
        change = f"Change: {(close - closes[i - 1]) / closes[i - 1] * 100:+.2f}%" if i else ""
        dt = datetime.fromtimestamp(start_ts + i * step).strftime("%Y-%m-%d %H:%M:%S")
        blocks.append(f"Time: {dt}\nOpen: {open_:.8f}\nHigh: {high:.8f}\nLow: {low:.8f}\n"
                      f"Close: {close:.8f}\nVolume: {1000.0:.2f}\n{change}\n---")
    return f"Historical OHLCV data for {symbol} ({timeframe}) on {exchange.upper()}:\n\n" + "\n".join(blocks)


def ccxt_summary_text(symbol: str, exchange: str, last: float) -> str:
    """Mirrors format_ticker in the same server."""
    return (f"Market summary for {symbol}:\n\nExchange: {exchange.upper()}\nSymbol: {symbol}\n"
            f"Last Price: {last}\n24h High: {last * 1.02}\n24h Low: {last * 0.98}\n24h Volume: 50000.0\n"
            f"Bid: {last * 0.9999}\nAsk: {last * 1.0001}\n---")


class FakeServers:
    def __init__(self, now: float) -> None:
        self.now = now
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.price: dict[str, float] = {}          # CEX pair -> last price override
        self.closes_1h: dict[str, list[float]] = {}  # CEX pair -> 1h closes ending at the last closed hour
        self.liquidity: dict[str, float] = {}       # token address -> liquidity_usd
        self.dex_price: dict[str, float] = {}
        self.dex_closes: dict[str, list[float]] = {}  # pool -> 24 hourly closes
        self.fail: set[str] = set()

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> ToolResult:
        assert (server, tool) in READ_ONLY_TOOLS, f"unexpected tool {server}.{tool}"
        self.calls.append((server, tool, args))
        if server == "ccxt":
            pair, ex = args["symbol"], args.get("exchange", "binance")
            if pair in self.fail:
                return ToolResult("Error accessing cryptocurrency data: binance GET 451", None)
            closes = self.closes_1h[pair]
            if tool == "get-market-summary":
                return ToolResult(ccxt_summary_text(pair, ex, self.price.get(pair, closes[-1])), None)
            hour = int(self.now // 3600 * 3600)
            if args["timeframe"] == "1h":
                # Last block is the still-forming hour; the agent must drop it.
                start = hour - len(closes) * 3600
                return ToolResult(ccxt_ohlcv_text(pair, ex, "1h", start, 3600, closes + [closes[-1]]), None)
            four = closes[3::4]
            start = hour - hour % (4 * 3600) - len(four) * 4 * 3600
            return ToolResult(ccxt_ohlcv_text(pair, ex, "4h", start, 4 * 3600, four + [four[-1]]), None)
        address = args.get("token_address")
        if tool == "getTokenDetails":
            return ToolResult("", {"id": address, "summary": {
                "price_usd": self.dex_price.get(address, 1.0), "liquidity_usd": self.liquidity.get(address, 5e6),
                "24h": {"volume_usd": 1e6}}})
        if tool == "getTokenPools":
            return ToolResult("", {"results": [{"id": f"pool-{address}", "volume_usd_24h": 1e6}], "has_next_page": False})
        pool = args["pool_address"]
        hour = int(self.now // 3600 * 3600)
        rows = []
        for i, c in enumerate(self.dex_closes[pool]):
            t = hour - (len(self.dex_closes[pool]) - i) * 3600
            rows.append({"time_open": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "time_close": datetime.fromtimestamp(t + 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "open": c, "high": c * 1.002, "low": c * 0.998, "close": c, "volume": 100})
        return ToolResult("", {"ohlcv": rows})
