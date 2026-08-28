"""End-of-run report: what a live or paper session actually achieved.

``status`` answers "what is it doing right now". This answers "was it worth it",
which is a harder question and needs a comparison the session itself never makes.

A session's own return means little in isolation. Up 2% is good if the market fell 5%
and bad if it rose 10%, so this refetches the bars covering the session's own window
and computes what simply holding would have returned over exactly the same hours. That
is the only honest yardstick, and it is the one every other part of this package
insists on.

It also reports cost drag annualised, because a short live run is precisely where a
ruinous fee rate is cheapest to notice and act on.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config

DAY_MS = 86_400_000


@dataclass
class SessionReport:
    name: str
    symbol: str
    interval: str
    strategy: str
    bars: int
    started: datetime | None
    updated: datetime | None
    equity: float
    position: float
    starting_cash: float
    costs: float
    trades: list[dict]
    halted: str | None = None
    benchmark_return_pct: float | None = None

    @property
    def days(self) -> float:
        if not (self.started and self.updated):
            return 0.0
        return (self.updated - self.started).total_seconds() / 86_400

    @property
    def return_pct(self) -> float:
        if self.starting_cash <= 0:
            return 0.0
        return (self.equity / self.starting_cash - 1.0) * 100.0

    @property
    def cost_drag_pct(self) -> float:
        return self.costs / self.starting_cash * 100.0 if self.starting_cash else 0.0

    @property
    def cost_drag_annual_pct(self) -> float:
        return self.cost_drag_pct / max(self.days / 365.0, 1.0 / 365.0)

    @property
    def gap(self) -> float | None:
        if self.benchmark_return_pct is None:
            return None
        return self.return_pct - self.benchmark_return_pct

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if float(t.get("net", 0) or 0) > 0)


def _parse_day(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def load(config: Config, name: str = "") -> SessionReport:
    """Read one session's state and trade log off disk."""
    state_path = Path(config.live.state_file)
    if not state_path.exists():
        raise FileNotFoundError(f"no session state at {state_path}")
    payload = json.loads(state_path.read_text(encoding="utf-8"))

    engine = payload.get("engine", {})
    book = engine.get("portfolio", {})
    trades: list[dict] = []
    trades_path = Path(config.live.trades_file) if config.live.trades_file else None
    if trades_path and trades_path.exists():
        with trades_path.open(newline="", encoding="utf-8") as handle:
            trades = list(csv.DictReader(handle))

    updated = datetime.fromtimestamp(payload.get("saved_at", 0) / 1000, tz=timezone.utc)
    # Prefer the recorded start time. Falling back to the first trade's date loses the
    # time of day, which turns a 30-minute session into most of a day.
    if payload.get("started_at"):
        started = datetime.fromtimestamp(payload["started_at"] / 1000, tz=timezone.utc)
    else:
        started = _parse_day(trades[0].get("opened", "")) if trades else None

    costs = float(book.get("fees_paid", 0.0)) + float(book.get("slippage_paid", 0.0))
    return SessionReport(
        name=name or payload.get("strategy", "session"),
        symbol=payload.get("symbol", "?"),
        interval=payload.get("interval", ""),
        strategy=payload.get("strategy", "?"),
        bars=int(engine.get("bars_seen", 0)),
        started=started,
        updated=updated,
        equity=float(book.get("cash", 0.0)),
        position=float(book.get("qty", 0.0)),
        starting_cash=config.account.starting_cash,
        costs=costs,
        trades=trades,
        halted=engine.get("risk", {}).get("halted_reason"),
    )


def mark_to_market(report: SessionReport, price: float) -> None:
    """Fold any open position into equity, so a holder is not reported as flat.

    A buy-and-hold session holds everything in the instrument and almost nothing in
    cash, so reporting cash alone would show it as having lost the lot.
    """
    report.equity += report.position * price


def render(reports: list[SessionReport], currency: str = "£") -> str:
    if not reports:
        return "\n  No sessions to report.\n"

    span = max(r.days for r in reports)
    bars = max(r.bars for r in reports)
    lines = [
        "",
        f"  Session report - {span:.1f} days, {bars:,} bars",
        "  " + "=" * 34,
        "",
        f"  {'strategy':<16} {'final':>10} {'return':>9} {'vs hold':>9} "
        f"{'trades':>7} {'wins':>6} {'costs':>9} {'cost/yr':>9}",
        "  " + "-" * 82,
    ]
    for r in sorted(reports, key=lambda x: -x.return_pct):
        gap = "" if r.gap is None else f"{r.gap:>+8.2f}p"
        win = f"{r.wins}/{len(r.trades)}" if r.trades else "-"
        lines.append(
            f"  {r.name:<16} {currency}{r.equity:>9,.2f} {r.return_pct:>+8.2f}% {gap:>9} "
            f"{len(r.trades):>7} {win:>6} {currency}{r.costs:>8,.2f} {r.cost_drag_annual_pct:>8,.0f}%"
            + ("  HALTED" if r.halted else "")
        )

    benchmark = next((r.benchmark_return_pct for r in reports if r.benchmark_return_pct is not None), None)
    if benchmark is not None:
        lines += ["", f"  The market itself moved {benchmark:+.2f}% over the same window."]

    lines += ["", "  What this does and does not show", "  " + "-" * 32]
    if span < 30:
        lines.append(
            f"  {span:.1f} days is far too short to judge a strategy. It samples one mood of"
        )
        lines.append(
            "  one market. Nothing here is evidence about returns - a month is a floor,"
        )
        lines.append("  and even then the backtests in this repository are the better guide.")
    else:
        lines.append(f"  {span:.0f} days is a usable sample for costs, still thin for returns.")

    worst = max(reports, key=lambda r: r.cost_drag_annual_pct)
    if worst.cost_drag_annual_pct >= 25 and worst.trades:
        lines += [
            "",
            f"  What it does show is cost. {worst.name} is paying "
            f"{worst.cost_drag_annual_pct:,.0f}% of capital a year",
            "  in fees. That figure is reliable after days, not months, because it does not",
            "  depend on which way the market went - and no entry signal survives it.",
        ]
    lines.append("")
    return "\n".join(lines)
