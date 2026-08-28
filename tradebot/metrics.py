"""Performance statistics.

The headline number most people look at is total return. The numbers that actually
tell you whether a strategy works are further down: gross versus net profit (how
much of your edge the venue took), max drawdown (how bad it got before it got
better), and trade count (how many chances the costs had to bite).

If ``gross_pnl`` is positive and ``net_pnl`` is negative, the strategy has a real
signal that is too small to pay for its own trading. That is the single most common
outcome in retail algorithmic trading, and the one this package is built to show you
before you fund an account rather than after.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .types import EquityPoint, Trade

MS_PER_YEAR = 365.0 * 24.0 * 60.0 * 60.0 * 1000.0


@dataclass
class Metrics:
    starting_equity: float
    ending_equity: float
    total_return_pct: float
    gross_pnl: float
    net_pnl: float
    fees_paid: float
    slippage_paid: float
    max_drawdown_pct: float
    sharpe: float
    trades: int
    win_rate: float
    gross_win_rate: float
    profit_factor: float
    avg_trade: float
    best_trade: float
    worst_trade: float
    bars: int
    halted_reason: str | None = None

    @property
    def total_costs(self) -> float:
        return self.slippage_paid + self.fees_paid

    @property
    def cost_share_of_gross(self) -> float:
        """Costs as a multiple of gross profit. Above 1.0 means they ate all of it."""
        if abs(self.gross_pnl) < 1e-12:
            return 0.0
        return self.total_costs / abs(self.gross_pnl)

    def render(self, title: str = "Results") -> str:
        lines = [
            "",
            f"  {title}",
            f"  {'=' * len(title)}",
            f"  Starting equity     {self.starting_equity:>14,.2f}",
            f"  Ending equity       {self.ending_equity:>14,.2f}",
            f"  Total return        {self.total_return_pct:>13.2f}%",
            "",
            f"  Gross P&L           {self.gross_pnl:>14,.2f}   the market move, mid to mid",
            f"  Spread + slippage   {-self.slippage_paid:>14,.2f}",
            f"  Commission          {-self.fees_paid:>14,.2f}",
            f"  Net P&L             {self.net_pnl:>14,.2f}   what you actually keep",
            "",
            f"  Trades              {self.trades:>14,}",
            f"  Win rate, on price  {self.gross_win_rate * 100:>13.1f}%   before costs",
            f"  Win rate, net       {self.win_rate * 100:>13.1f}%   after costs",
            f"  Profit factor       {self.profit_factor:>14.2f}",
            f"  Average trade       {self.avg_trade:>14,.2f}",
            f"  Best / worst        {self.best_trade:>14,.2f} / {self.worst_trade:,.2f}",
            "",
            f"  Max drawdown        {self.max_drawdown_pct:>13.2f}%",
            f"  Sharpe (annualised) {self.sharpe:>14.2f}",
            f"  Bars processed      {self.bars:>14,}",
        ]
        if self.halted_reason:
            lines.append(f"  Halted              {self.halted_reason:>14}")

        if self.trades and self.gross_win_rate - self.win_rate > 0.2:
            lines += [
                "",
                f"  >> {self.gross_win_rate:.0%} of these trades correctly predicted the direction, but only",
                f"     {self.win_rate:.0%} made money. The signal works; the trades are too small to",
                "     survive the cost of placing them.",
            ]
        elif self.gross_pnl > 0 and self.net_pnl < 0:
            lines += [
                "",
                "  >> This strategy found a real edge and then paid all of it, and more,",
                f"     to the venue. Costs were {self.cost_share_of_gross:.1f}x the gross profit.",
                "     Trade less often, or target bigger moves.",
            ]
        return "\n".join(lines) + "\n"


def _max_drawdown(curve: list[EquityPoint]) -> float:
    peak = -math.inf
    worst = 0.0
    for point in curve:
        peak = max(peak, point.equity)
        if peak > 0:
            worst = max(worst, 1.0 - point.equity / peak)
    return worst * 100.0


def _sharpe(curve: list[EquityPoint]) -> float:
    """Annualised Sharpe ratio, assuming a zero risk-free rate.

    Bar spacing is inferred from the equity curve's timestamps, so this works for
    any bar size without being told what it is.
    """
    if len(curve) < 3:
        return 0.0
    returns = []
    for prev, cur in zip(curve, curve[1:]):
        if prev.equity > 0:
            returns.append(cur.equity / prev.equity - 1.0)
    if len(returns) < 2:
        return 0.0

    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    stdev = variance**0.5
    if stdev < 1e-15:
        return 0.0

    span_ms = curve[-1].ts - curve[0].ts
    if span_ms <= 0:
        return 0.0
    bar_ms = span_ms / max(len(curve) - 1, 1)
    bars_per_year = MS_PER_YEAR / bar_ms
    return (mean / stdev) * math.sqrt(bars_per_year)


def summarise(
    curve: list[EquityPoint],
    trades: list[Trade],
    starting_equity: float,
    fees_paid: float,
    slippage_paid: float = 0.0,
    halted_reason: str | None = None,
) -> Metrics:
    ending = curve[-1].equity if curve else starting_equity
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    gross_wins_count = sum(1 for t in trades if t.gross_pnl > 0)
    gross_wins = sum(t.net_pnl for t in wins)
    gross_losses = abs(sum(t.net_pnl for t in losses))

    if gross_losses < 1e-12:
        profit_factor = float("inf") if gross_wins > 0 else 0.0
    else:
        profit_factor = gross_wins / gross_losses

    net_pnls = [t.net_pnl for t in trades]
    return Metrics(
        starting_equity=starting_equity,
        ending_equity=ending,
        total_return_pct=(ending / starting_equity - 1.0) * 100.0 if starting_equity else 0.0,
        gross_pnl=sum(t.gross_pnl for t in trades),
        net_pnl=ending - starting_equity,
        fees_paid=fees_paid,
        slippage_paid=slippage_paid,
        max_drawdown_pct=_max_drawdown(curve),
        sharpe=_sharpe(curve),
        trades=len(trades),
        win_rate=len(wins) / len(trades) if trades else 0.0,
        gross_win_rate=gross_wins_count / len(trades) if trades else 0.0,
        profit_factor=profit_factor,
        avg_trade=sum(net_pnls) / len(net_pnls) if net_pnls else 0.0,
        best_trade=max(net_pnls) if net_pnls else 0.0,
        worst_trade=min(net_pnls) if net_pnls else 0.0,
        bars=len(curve),
        halted_reason=halted_reason,
    )
