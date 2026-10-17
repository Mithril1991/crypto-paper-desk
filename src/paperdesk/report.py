"""Daily markdown report: trades, P&L, win rate, drawdown, and what went wrong."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import Settings
from .ledger import Ledger

DAY = 24 * 3600

CAVEATS = """## Read this before any number above

- These are **paper** fills: modelled fees and slippage, no real order book, no partial fills, no outages.
  Live results would be different, and usually worse.
- Stops and targets are checked once per cycle, not tick by tick, so a stop can fill well past its level.
- A few days or weeks of trades is a tiny sample. Nothing here shows the rule has an edge or that it
  will make money; treat every figure as a description of the past, not a prediction.
- No real orders are ever placed. The agent has no keys and no wallet.
"""


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def _day_bounds(day: date) -> tuple[float, float]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
    return start, start + DAY


def max_drawdown(series: list[float]) -> float:
    """Largest peak-to-trough fall, as a positive fraction."""
    peak, worst = float("-inf"), 0.0
    for v in series:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return worst


def build_report(ledger: Ledger, settings: Settings, day: date) -> str:
    db = ledger.db
    start, end = _day_bounds(day)
    eq_all = [r["equity"] for r in db.execute("SELECT equity FROM equity WHERE ts < ? ORDER BY ts", (end,))]
    eq_day = [r["equity"] for r in db.execute("SELECT equity FROM equity WHERE ts >= ? AND ts < ? ORDER BY ts", (start, end))]
    prev = db.execute("SELECT equity FROM equity WHERE ts < ? ORDER BY ts DESC LIMIT 1", (start,)).fetchone()
    eq_open = prev["equity"] if prev else settings.starting_balance_usd
    eq_close = eq_day[-1] if eq_day else eq_open

    closed_today = db.execute("SELECT * FROM positions WHERE exit_ts >= ? AND exit_ts < ? ORDER BY exit_ts", (start, end)).fetchall()
    opened_today = db.execute("SELECT * FROM positions WHERE entry_ts >= ? AND entry_ts < ? ORDER BY entry_ts", (start, end)).fetchall()
    closed_all = db.execute("SELECT pnl FROM positions WHERE exit_ts IS NOT NULL AND exit_ts < ?", (end,)).fetchall()
    wins_all = sum(1 for r in closed_all if r["pnl"] > 0)
    realized_all = sum(r["pnl"] for r in closed_all)
    realized_day = sum(r["pnl"] for r in closed_today)
    wins_day = sum(1 for r in closed_today if r["pnl"] > 0)
    fees_day = db.execute("SELECT COALESCE(SUM(fee), 0) AS f FROM fills WHERE ts >= ? AND ts < ?", (start, end)).fetchone()["f"]

    lines = [f"# Paper desk report {day.isoformat()} (UTC)", "",
             "Paper trading only. Not predictive. Not financial advice.", "",
             "## Summary", "",
             "| | Day | Since start |", "|---|---|---|",
             f"| Equity (close) | ${eq_close:,.2f} ({100 * (eq_close / eq_open - 1):+.2f}%) | "
             f"{100 * (eq_close / settings.starting_balance_usd - 1):+.2f}% vs ${settings.starting_balance_usd:,.0f} |",
             f"| Realized P&L | ${realized_day:,.2f} | ${realized_all:,.2f} |",
             f"| Closed trades | {len(closed_today)} | {len(closed_all)} |",
             f"| Win rate | {_rate(wins_day, len(closed_today))} | {_rate(wins_all, len(closed_all))} |",
             f"| Max drawdown | {100 * max_drawdown([eq_open, *eq_day]):.2f}% | {100 * max_drawdown([settings.starting_balance_usd, *eq_all]):.2f}% |",
             f"| Fees paid | ${fees_day:,.2f} | |", ""]
    if len(closed_all) < 30:
        lines += [f"_Only {len(closed_all)} closed trades so far: win rate and P&L are noise at this size._", ""]

    lines += ["## Trades", ""]
    if not opened_today and not closed_today:
        lines += ["No trades.", ""]
    else:
        lines += ["| Time | Symbol | Side | Price | Notes |", "|---|---|---|---|---|"]
        events = [(r["entry_ts"], r["symbol"], "BUY", r["entry_price"], _short(r["entry_reason"])) for r in opened_today]
        events += [(r["exit_ts"], r["symbol"], "SELL", r["exit_price"],
                    f"{r['exit_kind']}, P&L ${r['pnl']:,.2f}") for r in closed_today]
        for ts, sym, side, price, note in sorted(events):
            lines.append(f"| {_utc(ts)} | {sym} | {side} | {price:.6g} | {note} |")
        lines.append("")

    lines += ["## Open positions at close", ""]
    open_rows = db.execute("SELECT * FROM positions WHERE entry_ts < ? AND (exit_ts IS NULL OR exit_ts >= ?)", (end, end)).fetchall()
    if not open_rows:
        lines += ["None.", ""]
    else:
        lines += ["| Symbol | Entry | Mark | Unrealized | Stop | Target |", "|---|---|---|---|---|---|"]
        for r in open_rows:
            mark_row = db.execute("SELECT price FROM decisions WHERE symbol=? AND ts < ? AND price IS NOT NULL ORDER BY ts DESC LIMIT 1",
                                  (r["symbol"], end)).fetchone()
            mark = mark_row["price"] if mark_row else r["entry_price"]
            unreal = r["qty"] * (mark - r["entry_price"]) - r["entry_fee"]
            lines.append(f"| {r['symbol']} | {r['entry_price']:.6g} | {mark:.6g} | ${unreal:,.2f} | "
                         f"{r['stop_price']:.6g} | {r['take_price']:.6g} |")
        lines.append("")

    lines += ["## What the agent got wrong", ""] + _mistakes(ledger, settings, start, end) + [""]

    counts = db.execute("SELECT action, executed, COUNT(*) AS n FROM decisions WHERE ts >= ? AND ts < ? GROUP BY action, executed",
                        (start, end)).fetchall()
    errors = db.execute("SELECT COUNT(*) AS n FROM errors WHERE ts >= ? AND ts < ?", (start, end)).fetchone()["n"]
    lines += ["## Activity", ""]
    lines += [f"- {r['action']}{'' if r['executed'] or r['action'] in ('HOLD', 'SKIP') else ' (blocked)'}: {r['n']}" for r in counts]
    lines += [f"- Data errors: {errors}", "", CAVEATS]
    return "\n".join(lines)


def _mistakes(ledger: Ledger, s: Settings, start: float, end: float) -> list[str]:
    db = ledger.db
    out = []
    for r in db.execute("SELECT * FROM positions WHERE exit_ts >= ? AND exit_ts < ? AND pnl <= 0", (start, end)):
        loss_pct = 100 * (r["exit_price"] / r["entry_price"] - 1)
        note = f"- **{r['symbol']} lost ${-r['pnl']:,.2f}** ({loss_pct:+.2f}% price, exit: {r['exit_kind']}). Entry thesis: {_short(r['entry_reason'])}"
        if r["exit_kind"] == "stop" and loss_pct < -100 * s.stop_loss_pct - 0.5:
            note += f" The stop was {100 * s.stop_loss_pct:.0f}% but the fill was {loss_pct:.2f}%: price gapped between cycles."
        out.append(note)

    # Calls from the previous day, now that a full 24h of prices has followed them.
    p_start, p_end = start - DAY, end - DAY
    missed, early = {}, {}
    for d in db.execute("SELECT ts, symbol, price, action, executed FROM decisions WHERE ts >= ? AND ts < ? AND price IS NOT NULL",
                        (p_start, p_end)):
        hi = db.execute("SELECT MAX(price) AS hi FROM decisions WHERE symbol=? AND ts > ? AND ts <= ?",
                        (d["symbol"], d["ts"], d["ts"] + DAY)).fetchone()["hi"]
        if hi is None:
            continue
        gain = hi / d["price"] - 1
        if not d["executed"] and d["action"] in ("HOLD", "SKIP", "BUY") and gain >= s.take_profit_pct:
            missed.setdefault(d["symbol"], (d, gain))
        if d["executed"] and d["action"] == "SELL" and gain >= s.stop_loss_pct:
            early.setdefault(d["symbol"], (d, gain))
    for sym, (d, gain) in missed.items():
        why = "blocked BUY" if d["action"] == "BUY" else d["action"]
        out.append(f"- **Missed {sym}**: {why} at {_utc(d['ts'])} at {d['price']:.6g}; within 24h it traded {100 * gain:.1f}% higher (enough to reach the target).")
    for sym, (d, gain) in early.items():
        out.append(f"- **Early exit {sym}**: sold at {_utc(d['ts'])} at {d['price']:.6g}; within 24h it traded {100 * gain:.1f}% higher.")
    if not out:
        out.append("Nothing flagged. That is not evidence the rule works; it may only mean little happened.")
    out.append("")
    out.append("_Missed moves and early exits are judged with hindsight on the next 24h of cycle prices, "
               "which no rule could have known in advance; they show where the rule and the market disagreed._")
    return out


def _rate(wins: int, n: int) -> str:
    return "n/a" if n == 0 else f"{100 * wins / n:.0f}% ({wins}/{n})"


def _short(text: str, limit: int = 140) -> str:
    text = text.replace("|", "/")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def write_report(ledger: Ledger, settings: Settings, day: date, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{day.isoformat()}.md"
    path.write_text(build_report(ledger, settings, day))
    return path


def previous_utc_day(now: float) -> date:
    return (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=1)).date()
