from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from fakes import FakeServers, path

from paperdesk.agent import run_cycle
from paperdesk.config import Asset, CexRef, DexRef, Paths, Settings
from paperdesk.data import completed, parse_ccxt_ohlcv, parse_ccxt_summary, resample
from paperdesk.ledger import Ledger
from paperdesk.report import build_report, max_drawdown

NOW = datetime(2026, 10, 15, 12, 7, tzinfo=timezone.utc).timestamp()


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def cex(symbol: str, token: str | None = "0xtoken") -> Asset:
    dex = DexRef("ethereum", f"{token}{symbol}") if token else None
    return Asset(symbol, CexRef("binance", f"{symbol}/USDT"), dex)


@pytest.fixture
def env(tmp_path):
    paths = Paths.from_root(tmp_path)
    clock = Clock(NOW)
    ledger = Ledger(paths.db, 10_000, clock=clock)
    fake = FakeServers(NOW)
    yield paths, Settings(), ledger, fake, clock
    ledger.close()


def cycle(env, assets):
    paths, settings, ledger, fake, clock = env
    fake.now = clock.t
    return {o.symbol: o for o in asyncio.run(run_cycle(paths, settings, assets, fake, ledger, now=clock.t))}


def uptrend(fake, *symbols):
    for s in symbols:
        fake.closes_1h[f"{s}/USDT"] = path(168, 100.0, 0.0008, wiggle=0.006)


def test_parsers_read_the_ccxt_server_format():
    from fakes import ccxt_ohlcv_text, ccxt_summary_text
    q = parse_ccxt_summary(ccxt_summary_text("SOL/USDT", "binance", 150.5))
    assert q.last == 150.5 and q.ask > q.last > q.bid and q.volume_24h_usd == pytest.approx(50000 * 150.5)
    candles = parse_ccxt_ohlcv(ccxt_ohlcv_text("SOL/USDT", "binance", "1h", 1_700_000_000, 3600, [1.0, 2.0, 3.0]))
    assert [c.close for c in candles] == [1.0, 2.0, 3.0]
    assert candles[1].ts - candles[0].ts == 3600
    assert completed(candles, 3600, 1_700_000_000 + 3 * 3600 - 1)[-1].close == 2.0


def test_resample_keeps_only_full_buckets():
    from paperdesk.data import Candle
    hours = [Candle(4 * 3600 * 10 + i * 3600, i, i + 1, i - 1, i + 0.5, 1) for i in range(6)]
    four = resample(hours, 4 * 3600)
    assert len(four) == 1 and four[0].open == 0 and four[0].close == 3.5 and four[0].volume == 4


def test_settings_cannot_loosen_hard_risk_rules():
    for bad in ({"max_position_pct": 0.1}, {"max_open_positions": 4}, {"stop_loss_pct": 0.05},
                {"min_liquidity_usd": 100_000}):
        with pytest.raises(ValueError):
            Settings(**bad)


def test_buy_is_sized_at_five_percent_with_stop_and_target(env):
    paths, settings, ledger, fake, _ = env
    uptrend(fake, "BTC")
    out = cycle(env, [cex("BTC")])
    assert out["BTC"].action == "BUY" and out["BTC"].executed, out["BTC"].reason
    pos = ledger.open_position("BTC")
    cost = pos.qty * pos.entry_price + pos.entry_fee
    assert cost == pytest.approx(500, rel=1e-6)
    assert pos.stop_price == pytest.approx(pos.entry_price * 0.97)
    assert pos.take_price == pytest.approx(pos.entry_price * 1.06)
    assert ledger.cash() == pytest.approx(9_500, rel=1e-6)
    line = json.loads(paths.reasoning_log.read_text().splitlines()[-1])
    assert line["action"] == "BUY" and "4h uptrend" in line["reason"]


def test_never_more_than_three_open_positions(env):
    _, _, ledger, fake, _ = env
    syms = ["AAA", "BBB", "CCC", "DDD"]
    uptrend(fake, *syms)
    out = cycle(env, [cex(s) for s in syms])
    executed = [s for s, o in out.items() if o.action == "BUY" and o.executed]
    blocked = [o for o in out.values() if o.action == "BUY" and not o.executed]
    assert len(executed) == 3 and len(ledger.open_positions()) == 3
    assert len(blocked) == 1 and "max 3" in blocked[0].blocked_by


def test_stop_loss_closes_at_the_observed_price(env):
    _, _, ledger, fake, clock = env
    uptrend(fake, "BTC")
    cycle(env, [cex("BTC")])
    entry = ledger.open_position("BTC").entry_price
    clock.t += 900
    fake.price["BTC/USDT"] = entry * 0.95  # gapped through the 3% stop between cycles
    out = cycle(env, [cex("BTC")])
    assert out["BTC"].action == "SELL" and "Stop-loss" in out["BTC"].reason
    row = ledger.db.execute("SELECT * FROM positions").fetchone()
    assert row["exit_kind"] == "stop" and row["pnl"] < -500 * 0.03
    assert ledger.open_positions() == []
    assert ledger.cash() == pytest.approx(10_000 + row["pnl"])


def test_take_profit(env):
    _, _, ledger, fake, clock = env
    uptrend(fake, "BTC")
    cycle(env, [cex("BTC")])
    clock.t += 900
    fake.price["BTC/USDT"] = ledger.open_position("BTC").entry_price * 1.07
    out = cycle(env, [cex("BTC")])
    assert out["BTC"].action == "SELL" and "Take-profit" in out["BTC"].reason
    assert ledger.db.execute("SELECT pnl FROM positions").fetchone()["pnl"] > 0


def test_low_liquidity_token_is_skipped(env):
    _, _, ledger, fake, _ = env
    uptrend(fake, "BTC")
    fake.liquidity["0xtokenBTC"] = 400_000
    out = cycle(env, [cex("BTC")])
    assert out["BTC"].action == "SKIP" and "$400,000" in out["BTC"].reason
    assert ledger.open_positions() == []


def test_cex_only_asset_uses_volume_as_liquidity_proxy(env):
    _, _, ledger, fake, _ = env
    uptrend(fake, "BTC")
    out = cycle(env, [cex("BTC", token=None)])
    assert out["BTC"].executed
    assert not any(server == "dexpaprika" for server, _, _ in fake.calls)


def test_downtrend_holds_with_a_reason(env):
    _, _, ledger, fake, _ = env
    fake.closes_1h["BTC/USDT"] = path(168, 100.0, -0.0012)
    out = cycle(env, [cex("BTC")])
    assert out["BTC"].action == "HOLD" and "4h trend is down" in out["BTC"].reason


def test_one_failing_asset_does_not_stop_the_cycle(env):
    _, _, ledger, fake, _ = env
    uptrend(fake, "BTC", "ETH")
    fake.fail.add("ETH/USDT")
    out = cycle(env, [cex("BTC"), cex("ETH")])
    assert "BTC" in out and "ETH" not in out
    assert ledger.db.execute("SELECT COUNT(*) FROM errors").fetchone()[0] == 1


def test_dex_only_asset_warms_up_then_respects_cache(env):
    _, _, ledger, fake, clock = env
    jup = Asset("JUP", None, DexRef("solana", "JUPmint"))
    fake.dex_closes["pool-JUPmint"] = path(24, 1.0, 0.002)
    out = cycle(env, [jup])
    assert out["JUP"].action == "HOLD" and "Warming up" in out["JUP"].reason
    assert len(ledger.candles("JUP", 0)) == 24
    calls = len(fake.calls)
    clock.t += 900
    cycle(env, [jup])
    tools = [t for _, t, _ in fake.calls[calls:]]
    assert tools == ["getTokenDetails"]  # pool and candles come from the 60-min cache


def test_report_has_required_sections_and_flags_losses(env):
    _, settings, ledger, fake, clock = env
    uptrend(fake, "BTC")
    cycle(env, [cex("BTC")])
    clock.t += 900
    fake.price["BTC/USDT"] = ledger.open_position("BTC").entry_price * 0.95
    cycle(env, [cex("BTC")])
    text = build_report(ledger, settings, datetime.fromtimestamp(NOW, timezone.utc).date())
    for section in ("## Summary", "## Trades", "Win rate", "Max drawdown", "## What the agent got wrong", "not predictive".capitalize()):
        assert section.lower() in text.lower()
    assert "BTC lost" in text and "gapped" in text
    assert "0% (0/1)" in text


def test_max_drawdown():
    assert max_drawdown([100, 120, 90, 130, 117]) == pytest.approx(0.25)
    assert max_drawdown([100, 101, 102]) == 0
