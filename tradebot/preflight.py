"""Readiness checks for live trading.

The four gates in ``brokers/cryptocom.py`` are mechanical: they stop an *accident*.
They cannot tell you whether going live is a good idea, because a config flag knows
nothing about whether the strategy works or whether you have ever tested it.

This module asks the substantive questions instead, and answers them from evidence
already on disk: have you paper traded, for how long, did it make money, and does the
strategy beat simply holding the asset? Those are the questions worth failing.

Nothing here can stop you trading. It is a checklist that reports honestly, so the
decision is made against facts rather than optimism.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config

#: Paper trading shorter than this tells you almost nothing - you have sampled one
#: market mood, not a strategy. A month is a floor, not a target.
MIN_PAPER_DAYS = 30
MIN_PAPER_TRADES = 20

#: A paper record that stopped weeks ago describes a market that no longer exists, and
#: is also how a stale or stray file passes for evidence. Either way it is not a
#: reason to risk money today.
MAX_RECORD_AGE_DAYS = 7

#: Costs above this share of capital per year are fatal on their own. Measured over
#: 2.3 years of hourly crypto and 208 days of 15-minute crypto, the active strategies
#: in this package ran at 24-148% a year against a buy-and-hold benchmark paying 0.1%.
#: None of them beat holding, in 24 runs out of 24. There is no entry signal good
#: enough to outrun a number like that.
MAX_ANNUAL_COST_DRAG_PCT = 20.0


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    blocking: bool = True

    @property
    def mark(self) -> str:
        if self.passed:
            return "PASS"
        return "FAIL" if self.blocking else "WARN"


def _read_paper_trades(path: str) -> list[dict]:
    target = Path(path)
    if not target.exists():
        return []
    try:
        with target.open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except (OSError, csv.Error):
        return []


def _newest(rows: list[dict]) -> datetime | None:
    dates = []
    for row in rows:
        try:
            dates.append(datetime.strptime(row["closed"], "%Y-%m-%d").replace(tzinfo=timezone.utc))
        except (KeyError, ValueError):
            continue
    return max(dates) if dates else None


def _span_days(rows: list[dict]) -> float:
    dates = []
    for row in rows:
        for key in ("opened", "closed"):
            try:
                dates.append(datetime.strptime(row[key], "%Y-%m-%d").replace(tzinfo=timezone.utc))
            except (KeyError, ValueError):
                continue
    if len(dates) < 2:
        return 0.0
    return (max(dates) - min(dates)).total_seconds() / 86_400


def credentials_present() -> bool:
    return bool(os.environ.get("CRYPTOCOM_API_KEY") and os.environ.get("CRYPTOCOM_API_SECRET"))


def run(
    config: Config,
    backtest_verdict: tuple[bool, str] | None = None,
    annual_cost_drag_pct: float | None = None,
) -> list[Check]:
    """Every readiness check, in the order they matter."""
    checks: list[Check] = []

    if annual_cost_drag_pct is not None:
        ok = annual_cost_drag_pct <= MAX_ANNUAL_COST_DRAG_PCT
        checks.append(Check(
            "Cost drag",
            ok,
            f"costs run at {annual_cost_drag_pct:.1f}% of capital per year"
            + ("" if ok else
               f", against a {MAX_ANNUAL_COST_DRAG_PCT:.0f}% limit. Holding the same asset "
               "pays about 0.1%. No entry signal is good enough to outrun this - "
               "trade less often or target bigger moves."),
        ))

    rows = _read_paper_trades(config.live.trades_file)
    days = _span_days(rows)
    net = sum(float(r.get("net", 0) or 0) for r in rows)

    if not rows:
        checks.append(Check(
            "Paper trading",
            False,
            "no paper trades on record. Run `tradebot paper` for at least a month "
            "before risking anything.",
        ))
    elif len(rows) < MIN_PAPER_TRADES or days < MIN_PAPER_DAYS:
        checks.append(Check(
            "Paper trading",
            False,
            f"only {len(rows)} trades over {days:.0f} days. "
            f"Want at least {MIN_PAPER_TRADES} trades over {MIN_PAPER_DAYS} days - "
            "a shorter run samples one market mood, not a strategy.",
        ))
    else:
        checks.append(Check(
            "Paper trading",
            True,
            f"{len(rows)} trades over {days:.0f} days, net {net:+,.2f}",
        ))

    newest = _newest(rows)
    if newest is not None:
        age = (datetime.now(tz=timezone.utc) - newest).total_seconds() / 86_400
        checks.append(Check(
            "Record is current",
            age <= MAX_RECORD_AGE_DAYS,
            f"most recent trade closed {newest:%Y-%m-%d}"
            + ("" if age <= MAX_RECORD_AGE_DAYS else
               f", {age:.0f} days ago. That describes a market that has moved on - and "
               "a stale or stray file is how junk passes for evidence. Re-run paper "
               "trading before trusting it."),
        ))

    if rows and net <= 0:
        checks.append(Check(
            "Paper result",
            False,
            f"paper trading lost {abs(net):,.2f}. A strategy that loses on simulated "
            "fills will not start winning on real ones - real fills are worse.",
        ))
    elif rows:
        checks.append(Check("Paper result", True, f"paper trading made {net:+,.2f}"))

    if backtest_verdict is not None:
        beat, detail = backtest_verdict
        checks.append(Check("Beats buy-and-hold", beat, detail))

    checks.append(Check(
        "API credentials",
        credentials_present(),
        "CRYPTOCOM_API_KEY and CRYPTOCOM_API_SECRET are set"
        if credentials_present()
        else "not set. Export them in your shell - never put them in config.toml.",
    ))

    checks.append(Check(
        "Live gates",
        config.live.enabled and not config.live.dry_run,
        "armed"
        if config.live.enabled and not config.live.dry_run
        else f"enabled={config.live.enabled}, dry_run={config.live.dry_run} "
             "(this is the safe setting - nothing will be sent)",
        blocking=False,
    ))

    cap = config.live.max_order_notional
    cash = config.account.starting_cash
    checks.append(Check(
        "Order size cap",
        cap <= max(cash * 0.1, 25.0),
        f"max_order_notional is {cap:,.2f} against an account of {cash:,.2f}. "
        + ("Sensible for a first live run." if cap <= max(cash * 0.1, 25.0)
           else "Start far smaller - the smallest order the venue accepts."),
        blocking=False,
    ))

    limits = config.risk
    weak = limits.max_drawdown_pct >= 0.5 or limits.max_position_pct > 0.5
    checks.append(Check(
        "Risk limits",
        not weak,
        f"position cap {limits.max_position_pct:.0%}, daily loss {limits.max_daily_loss_pct:.0%}, "
        f"kill switch {limits.max_drawdown_pct:.0%}"
        + ("" if not weak else "  - these are too loose to leave running unattended"),
    ))

    checks.append(Check(
        "Out-of-sample proof",
        False,
        "no walk-forward result recorded. An in-sample backtest is the best case by "
        "construction - this package's own best candidate gave up 80% of its apparent "
        "edge when the parameters were chosen without seeing the test data. Run "
        "`tradebot walkforward` and judge by that column.",
        blocking=False,
    ))

    checks.append(Check(
        "Durable host",
        False,
        "a live bot must run somewhere that stays up. If the machine stops, the bot "
        "stops holding a position it can no longer manage or exit. Cloud sessions and "
        "laptops that sleep do not qualify.",
        blocking=False,
    ))

    return checks


def render(checks: list[Check]) -> str:
    lines = ["", "  Live trading readiness", "  " + "=" * 22, ""]
    for check in checks:
        lines.append(f"  [{check.mark}] {check.name}")
        lines.append(f"         {check.detail}")
        lines.append("")

    blocking = [c for c in checks if not c.passed and c.blocking]
    warnings = [c for c in checks if not c.passed and not c.blocking]

    if blocking:
        lines.append(f"  {len(blocking)} blocking issue(s): "
                     + ", ".join(c.name.lower() for c in blocking))
        lines.append("  Do not trade real money until these are resolved.")
    else:
        lines.append("  No blocking issues.")
        if warnings:
            lines.append(f"  Still worth reading: {', '.join(c.name.lower() for c in warnings)}.")
        lines.append("")
        lines.append("  Passing this checklist is not a prediction that you will make money.")
        lines.append("  It only means you have done the testing that makes the risk informed.")
    lines.append("")
    return "\n".join(lines)
