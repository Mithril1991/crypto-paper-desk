"""Settings, watchlist and MCP server definitions, all read from the repo root."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    starting_balance_usd: float = 10_000.0
    max_position_pct: float = 0.05
    max_open_positions: int = 3
    stop_loss_pct: float = 0.03
    take_profit_pct: float = 0.06
    min_liquidity_usd: float = 500_000.0
    cycle_minutes: int = 15
    fee_bps: float = 10.0
    cex_slippage_bps: float = 5.0
    dex_slippage_bps: float = 30.0
    dex_cache_minutes: int = 60
    dex_pool_cache_hours: int = 24
    volume_spike_ratio: float = 2.0
    max_atr_pct_1h: float = 2.0
    rsi_entry_min: float = 50.0
    rsi_entry_max: float = 70.0

    def __post_init__(self) -> None:
        # The risk rules are hard limits: a config file may tighten them, never loosen them.
        if self.max_position_pct > 0.05:
            raise ValueError("max_position_pct may not exceed 0.05")
        if self.max_open_positions > 3:
            raise ValueError("max_open_positions may not exceed 3")
        if self.stop_loss_pct > 0.03 or self.stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct must be in (0, 0.03]")
        if self.take_profit_pct <= 0:
            raise ValueError("take_profit_pct must be positive")
        if self.min_liquidity_usd < 500_000:
            raise ValueError("min_liquidity_usd may not be below 500000")

    @classmethod
    def load(cls, path: Path) -> Settings:
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text())
        known = {f.name for f in fields(cls)}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"unknown settings in {path}: {sorted(unknown)}")
        return cls(**raw)


@dataclass(frozen=True)
class CexRef:
    exchange: str
    pair: str


@dataclass(frozen=True)
class DexRef:
    network: str
    token_address: str
    pool_address: str | None = None


@dataclass(frozen=True)
class Asset:
    symbol: str
    cex: CexRef | None
    dex: DexRef | None

    @property
    def dex_only(self) -> bool:
        return self.cex is None


def load_watchlist(path: Path) -> list[Asset]:
    raw = json.loads(path.read_text())
    assets = []
    for row in raw["assets"]:
        cex = CexRef(**row["cex"]) if row.get("cex") else None
        dex = DexRef(**row["dex"]) if row.get("dex") else None
        if cex is None and dex is None:
            raise ValueError(f"{row['symbol']}: needs a cex or a dex source")
        assets.append(Asset(symbol=row["symbol"], cex=cex, dex=dex))
    return assets


@dataclass(frozen=True)
class ServerSpec:
    command: str
    args: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()


def load_mcp_servers(path: Path) -> dict[str, ServerSpec]:
    """Read the same .mcp.json Claude Code uses, so the agent and Claude see one setup."""
    raw = json.loads(path.read_text())["mcpServers"]
    return {
        name: ServerSpec(
            command=spec["command"],
            args=tuple(spec.get("args", [])),
            env=tuple(sorted(spec.get("env", {}).items())),
        )
        for name, spec in raw.items()
    }


@dataclass(frozen=True)
class Paths:
    root: Path
    config: Path
    watchlist: Path
    mcp: Path
    db: Path
    reasoning_log: Path
    reports: Path

    @classmethod
    def from_root(cls, root: Path) -> Paths:
        root = root.resolve()
        return cls(
            root=root,
            config=root / "config.json",
            watchlist=root / "watchlist.json",
            mcp=root / ".mcp.json",
            db=root / "data" / "ledger.sqlite",
            reasoning_log=root / "data" / "reasoning.jsonl",
            reports=root / "reports",
        )
