# CLAUDE.md - crypto-paper-desk

Paper-trading research agent. See README.md.

## Hard rules
- Market data comes only from the `dexpaprika` and `ccxt` MCP servers in `.mcp.json`. No direct
  exchange or chain calls, no API keys for exchanges, no wallets or private keys, no order tools.
- Never place real orders. SELL only closes a paper position.
- The risk limits in `Settings.__post_init__` may be tightened, never loosened.
- Never describe results as predictive or as an edge.

## Conventions
- Python 3.11+, `from __future__ import annotations`, frozen dataclasses, stdlib plus `mcp`.
- Tests: `pytest`; no network. `tests/fakes.py` mirrors the real servers' output formats; if a
  server changes its format, update the fake from the server source, not from memory.
