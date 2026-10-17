# crypto-paper-desk

A 24/7 crypto research agent that watches a watchlist, decides BUY / SELL / HOLD with a
written reason, records **paper** trades in a local SQLite ledger, and writes a daily
markdown report.

- **Paper only.** No exchange API keys, no wallets, no private keys, no order tools. The
  agent reads data through two read-only MCP servers and nothing else.
- **Not predictive.** The rule is a plain trend filter chosen to be readable, not because it
  is known to work. Run it for weeks before reading anything into a number, and even then
  paper results are not live results. Not financial advice.

## Data: two MCP servers, nothing else

`.mcp.json` defines both servers. The agent starts them itself over stdio each cycle, and
Claude Code picks up the same file, so you can ask Claude about the market with the same tools.

| Server | Used for | Tools called |
|---|---|---|
| [DexPaprika MCP](https://github.com/coinpaprika/dexpaprika-mcp) (`npx dexpaprika-mcp`) | DEX liquidity for the $500k gate; price and 1h candles for DEX-only tokens | `getTokenDetails`, `getTokenPools`, `getPoolOHLCV` |
| [CCXT MCP server](https://github.com/Nayshins/mcp-server-ccxt) (`uvx … ccxt-server`) | CEX price, bid/ask, 1h and 4h candles | `get-market-summary`, `get-historical-ohlcv` |

The CCXT server is pinned to `ccxt==4.5.20` and `mcp==1.30.0` in `.mcp.json`: with the
latest of either it crashes on startup (`ccxt.huobi` was removed; `Server.list_tools` is gone
in mcp 2.x).

**DexPaprika keyless budget.** Keyless use is 10,000 credits per rolling 30 days and
15 requests/min. Liquidity is cached for `dex_cache_minutes` (60), so an asset with a CEX
pair costs about 24 calls a day; a DEX-only asset costs about 120 (a fresh price every
cycle plus hourly candles). The default watchlist uses about 5,800 credits per 30 days.
Adding more DEX-only tokens will run out; a free key (`DEXPAPRIKA_API_KEY` in the
server's `env` in `.mcp.json`) raises the allowance.

## Setup

Needs Python 3.11+, Node (for `npx`) and [uv](https://docs.astral.sh/uv/) (for `uvx`).

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/paperdesk once      # one cycle now; starts both MCP servers
.venv/bin/paperdesk run       # every 15 minutes, forever
.venv/bin/paperdesk status    # cash, equity, open positions
.venv/bin/paperdesk report --date 2026-10-17   # UTC day; default is yesterday
.venv/bin/pytest
```

To keep it running: `deploy/paperdesk.service` is a systemd user unit (edit the paths).
`run` also writes yesterday's report automatically after the first cycle of each UTC day.

## The loop (every 15 minutes)

1. Read `watchlist.json` (edits apply next cycle). For each asset pull price, bid/ask,
   24h volume, and 1h (7 days) and 4h (14 days) candles. The still-forming candle is dropped.
2. Compute trend (4h close vs EMA20 and its slope; 1h EMA20 vs EMA50), momentum (RSI14,
   24h change), volatility (ATR14 %, realized vol), and flag a volume spike when the last
   1h volume is ≥ 2x the 24h median. Liquidity comes from DexPaprika when the asset has a
   `dex` mapping; a CEX-only asset falls back to 24h CEX volume, and the report says so.
3. Decide, with the reason stored for every asset every cycle:
   - **SELL** an open position on stop-loss, take-profit, or 4h downtrend plus 1h close below EMA50.
   - **BUY** with no position when the 4h trend is up, 1h EMA20 > EMA50, RSI 50-70 and
     1h ATR ≤ 2% (a 3% stop inside the hourly noise is a coin flip).
   - otherwise **HOLD** (or **SKIP** under the liquidity gate), naming which condition failed.
4. Apply paper fills and write `data/ledger.sqlite` and `data/reasoning.jsonl`.

DEX-only tokens: keyless DexPaprika serves only the last 24 hours of 1h candles, so the
agent stores them and builds its own history. Such a token stays in "Warming up" for
about four days until it has enough 4h candles.

## Risk rules (hard)

Set in `config.json`. `Settings` refuses any value looser than these, so they can be
tightened but not relaxed.

- $10,000 starting paper balance
- max 5% of equity per position (notional plus fee), max 3 open positions
- stop-loss 3%, take-profit 6% from the fill price; no leverage, no shorting (SELL only closes)
- skip any token with < $500k liquidity

## Making paper fills less flattering

- Fees: 10 bps per side (`fee_bps`).
- Slippage: buys fill above the ask and sells below the bid, by 5 bps on a CEX and 30 bps
  on a DEX.
- Stops are checked once per cycle at the observed price, not at the stop level, so a
  move between cycles shows up as a loss beyond 3%. The report flags these.

## Daily report (`reports/YYYY-MM-DD.md`)

Equity and return, realized P&L, closed trades, win rate, max drawdown (day and since
start), fees, every trade with its reason, open positions with unrealized P&L, and
**What the agent got wrong**:

- each losing trade, with the entry reason that led to it and the exit kind
- stops that filled well beyond 3% (gap between cycles)
- missed moves: a HOLD/SKIP/blocked BUY the previous day that was followed by a rise of 6% or more within 24h
- early exits: a SELL followed by a rise of 3% or more within 24h

Below 30 closed trades the report says the win rate and P&L are noise, because they are.

## Layout

```
.mcp.json            the two MCP servers (shared with Claude Code)
config.json          risk and cost settings
watchlist.json       assets: cex {exchange, pair} and/or dex {network, token_address[, pool_address]}
src/paperdesk/
  data.py            MCP client and parsers (ccxt answers in text, dexpaprika in JSON)
  indicators.py      EMA, RSI, ATR, realized vol, volume ratio
  strategy.py        features and the BUY/SELL/HOLD rule with reasons
  ledger.py          SQLite: account, positions, fills, decisions, equity, errors, candles, cache
  agent.py           one cycle: data, decide, risk gates, paper fills
  report.py          daily markdown report
  cli.py             once | run | report | status
tests/               fake MCP servers that answer in the real servers' formats
```
