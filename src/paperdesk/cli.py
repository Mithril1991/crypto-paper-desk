"""paperdesk once | run | report | status"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from datetime import date
from pathlib import Path

from .agent import run_cycle
from .config import Paths, Settings, load_mcp_servers, load_watchlist
from .data import McpToolCaller
from .ledger import Ledger
from .report import previous_utc_day, write_report

log = logging.getLogger("paperdesk")


def _setup(root: Path) -> tuple[Paths, Settings, Ledger]:
    paths = Paths.from_root(root)
    settings = Settings.load(paths.config)
    return paths, settings, Ledger(paths.db, settings.starting_balance_usd)


async def _cycle(paths: Paths, settings: Settings, ledger: Ledger) -> None:
    assets = load_watchlist(paths.watchlist)  # re-read each cycle so edits apply without a restart
    async with McpToolCaller(load_mcp_servers(paths.mcp)) as caller:
        await run_cycle(paths, settings, assets, caller, ledger, now=time.time())
    log.info("equity $%.2f, cash $%.2f, open %d", ledger.last_equity() or 0, ledger.cash(), len(ledger.open_positions()))


def _catch_up_report(paths: Paths, settings: Settings, ledger: Ledger) -> None:
    day = previous_utc_day(time.time())
    started = ledger.db.execute("SELECT started_at FROM account").fetchone()["started_at"]
    if not (paths.reports / f"{day.isoformat()}.md").exists() and started < time.time() - 3600:
        log.info("wrote %s", write_report(ledger, settings, day, paths.reports))


def cmd_run(paths: Paths, settings: Settings, ledger: Ledger) -> None:
    period = settings.cycle_minutes * 60
    while True:
        try:
            asyncio.run(_cycle(paths, settings, ledger))
        except Exception:  # keep running 24/7; the error is in the log and the next cycle retries
            log.exception("cycle failed")
            ledger.record_error(None, "cycle failed; see log")
        try:
            _catch_up_report(paths, settings, ledger)
        except Exception:
            log.exception("report failed")
        time.sleep(period - time.time() % period + 5)  # wake just after the next boundary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="paperdesk", description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repo root holding config.json, watchlist.json, .mcp.json")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("once", help="run one cycle now")
    sub.add_parser("run", help="run a cycle every cycle_minutes, forever")
    rep = sub.add_parser("report", help="write the daily markdown report")
    rep.add_argument("--date", type=date.fromisoformat, help="UTC date, default yesterday")
    sub.add_parser("status", help="print cash, equity and open positions")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    paths, settings, ledger = _setup(args.root)
    if args.cmd == "once":
        asyncio.run(_cycle(paths, settings, ledger))
    elif args.cmd == "run":
        cmd_run(paths, settings, ledger)
    elif args.cmd == "report":
        day = args.date or previous_utc_day(time.time())
        print(write_report(ledger, settings, day, paths.reports))
    elif args.cmd == "status":
        print(f"cash ${ledger.cash():,.2f}  last equity ${ledger.last_equity() or settings.starting_balance_usd:,.2f}")
        for p in ledger.open_positions():
            print(f"{p.symbol:6} qty {p.qty:.6g} entry {p.entry_price:.6g} stop {p.stop_price:.6g} target {p.take_price:.6g}")
    return 0
