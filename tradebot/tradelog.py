"""Rendering and recording the trade log.

The metrics tell you how a strategy did. The trade log tells you *what it actually
did* - every position it opened, what it paid, how long it held, and what it kept.
That is the difference between believing a number and being able to check it.

The same rows go to three places: a table in the terminal, a CSV you can open in a
spreadsheet, and an append-as-you-go file that a live or paper run writes after every
closed trade, so a session's history survives a restart.
"""

from __future__ import annotations

import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .types import Trade

COLUMNS = [
    "n", "opened", "closed", "days", "side", "qty",
    "entry", "exit", "gross", "costs", "net", "balance", "reason",
]

_GREEN = "\033[32m"
_RED = "\033[31m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _colour_enabled(stream=None) -> bool:
    """Colour only for a real terminal, and never when NO_COLOR is set."""
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _duration_days(trade: Trade) -> float:
    return (trade.exit_ts - trade.entry_ts) / 86_400_000


def rows(trades: list[Trade], starting_cash: float = 0.0) -> list[dict]:
    """Trades as plain dictionaries, with a running balance."""
    balance = starting_cash
    out = []
    for index, trade in enumerate(trades, start=1):
        balance += trade.net_pnl
        out.append(
            {
                "n": index,
                "opened": _day(trade.entry_ts),
                "closed": _day(trade.exit_ts),
                "days": round(_duration_days(trade), 1),
                "side": trade.side.value,
                "qty": round(trade.qty, 8),
                "entry": round(trade.entry_price, 4),
                "exit": round(trade.exit_price, 4),
                "gross": round(trade.gross_pnl, 2),
                "costs": round(trade.total_cost, 2),
                "net": round(trade.net_pnl, 2),
                "balance": round(balance, 2),
                "reason": trade.reason,
            }
        )
    return out


def render(
    trades: list[Trade],
    starting_cash: float = 0.0,
    currency: str = "",
    limit: int | None = None,
    colour: bool | None = None,
) -> str:
    """A readable table of every trade, newest last."""
    if not trades:
        return "\n  No trades were made.\n"

    data = rows(trades, starting_cash)
    shown = data if limit is None else data[:limit]
    use_colour = _colour_enabled() if colour is None else colour

    header = (
        f"  {'#':>4}  {'opened':<10} {'closed':<10} {'days':>6} "
        f"{'qty':>12} {'entry':>10} {'exit':>10} "
        f"{'gross':>10} {'costs':>8} {'net':>10} {'balance':>11}  reason"
    )
    lines = ["", header, "  " + "-" * (len(header) - 2)]

    for row in shown:
        net = row["net"]
        marker = "+" if net > 0 else ""
        body = (
            f"  {row['n']:>4}  {row['opened']:<10} {row['closed']:<10} {row['days']:>6,.0f} "
            f"{row['qty']:>12,.4f} {row['entry']:>10,.2f} {row['exit']:>10,.2f} "
            f"{row['gross']:>+10,.2f} {row['costs']:>8,.2f} {marker}{net:>9,.2f} "
            f"{currency}{row['balance']:>10,.2f}  {row['reason'][:44]}"
        )
        if use_colour:
            body = f"{_GREEN if net > 0 else _RED}{body}{_RESET}"
        lines.append(body)

    if limit is not None and len(data) > limit:
        remaining = len(data) - limit
        note = f"  ... and {remaining:,} more (use --limit 0 to see all, or --csv to export)"
        lines.append(f"{_DIM}{note}{_RESET}" if use_colour else note)

    lines.append("")
    lines.append(summary(trades, starting_cash, currency))
    return "\n".join(lines) + "\n"


def summary(trades: list[Trade], starting_cash: float = 0.0, currency: str = "") -> str:
    """The totals under the table, so the columns can be checked against them."""
    if not trades:
        return "  No trades."

    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    net = sum(t.net_pnl for t in trades)
    costs = sum(t.total_cost for t in trades)
    gross = sum(t.gross_pnl for t in trades)
    durations = [_duration_days(t) for t in trades]

    avg_win = sum(t.net_pnl for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.net_pnl for t in losses) / len(losses) if losses else 0.0

    lines = [
        f"  {len(trades):,} trades: {len(wins):,} winners, {len(losses):,} losers "
        f"({len(wins) / len(trades) * 100:.1f}% win rate)",
        f"  Gross {currency}{gross:,.2f}  -  costs {currency}{costs:,.2f}  "
        f"=  net {currency}{net:,.2f}",
        f"  Average winner {currency}{avg_win:,.2f}, average loser {currency}{avg_loss:,.2f}, "
        f"held {sum(durations) / len(durations):,.0f} days on average",
    ]
    if starting_cash:
        lines.append(
            f"  Balance {currency}{starting_cash:,.2f} -> {currency}{starting_cash + net:,.2f}"
        )
    longest = max(trades, key=_duration_days)
    if _duration_days(longest) >= 1:
        lines.append(
            f"  Longest hold: {_duration_days(longest):,.0f} days "
            f"({_day(longest.entry_ts)} to {_day(longest.exit_ts)}), "
            f"net {currency}{longest.net_pnl:,.2f}"
        )
    return "\n".join(lines)


def write_csv(path: str | Path, trades: list[Trade], starting_cash: float = 0.0) -> Path:
    """Export the whole log, for a spreadsheet or your own analysis."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows(trades, starting_cash))
    return target


def append(path: str | Path, trade: Trade, balance: float, number: int | None = None) -> None:
    """Record one closed trade as it happens, for a live or paper run.

    Appends rather than rewrites, and writes the header only once, so a session's
    history survives restarts and can be tailed while the bot is running.

    ``number`` is the trade's position in the session. Left out, it is counted from
    the rows already in the file, so a resumed run keeps numbering where it stopped
    instead of restarting at 1 on every append.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    exists = target.exists() and target.stat().st_size > 0

    if number is None:
        number = 1
        if exists:
            with target.open(newline="", encoding="utf-8") as handle:
                number = max(1, sum(1 for _ in handle))  # header occupies one line

    with target.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        if not exists:
            writer.writeheader()
        row = rows([trade])[0]
        row["n"] = number
        row["balance"] = round(balance, 2)
        writer.writerow(row)
