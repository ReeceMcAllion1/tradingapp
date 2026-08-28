"""Replay a strategy over historical bars.

A backtest is a hypothesis test, not a promise. Its result is the best case: real
trading adds outages, partial fills, funding costs, thinner liquidity than the tape
suggests, and the fact that you chose this strategy *after* seeing this data. Treat a
backtest that barely breaks even as a losing strategy, because that is what it will
become.

What this backtester does do honestly:

* charges spread, slippage and commission on every fill;
* executes on the bar *after* the decision, never on the bar that produced it;
* resolves ambiguous stop-and-target bars against you;
* reports gross and net profit separately, so cost drag is impossible to miss.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config
from .costs import CostModel
from .engine import Engine, ExecutionSettings
from .metrics import Metrics, summarise
from .portfolio import Portfolio
from .risk import RiskLimits, RiskManager
from .strategies.base import Strategy
from .types import Candle, Trade


@dataclass
class BacktestResult:
    metrics: Metrics
    trades: list[Trade]
    engine: Engine

    @property
    def risk_events(self) -> list[str]:
        return self.engine.risk.events


def run(
    candles: list[Candle],
    strategy: Strategy,
    starting_cash: float = 1000.0,
    costs: CostModel | None = None,
    limits: RiskLimits | None = None,
    execution: ExecutionSettings | None = None,
) -> BacktestResult:
    """Replay ``candles`` through ``strategy`` and report what would have happened."""
    if not candles:
        raise ValueError("cannot backtest an empty series")

    costs = costs or CostModel()
    limits = limits or RiskLimits()
    execution = execution or ExecutionSettings()

    portfolio = Portfolio(starting_cash=starting_cash)
    risk = RiskManager(limits=limits, costs=costs)
    engine = Engine(
        strategy=strategy,
        portfolio=portfolio,
        risk=risk,
        costs=costs,
        execution=execution,
    )

    for candle in candles:
        engine.process(candle)

    # Close out at the end so the reported result is cash, not an open position whose
    # value depends on where the series happened to stop.
    final = candles[-1]
    if not portfolio.is_flat:
        engine.close_position(final.close, final.ts, "end of backtest")
        portfolio.equity_curve[-1].equity = portfolio.equity(final.close)
        portfolio.equity_curve[-1].position = portfolio.qty
        portfolio.equity_curve[-1].cash = portfolio.cash

    metrics = summarise(
        curve=portfolio.equity_curve,
        trades=portfolio.trades,
        starting_equity=starting_cash,
        fees_paid=portfolio.fees_paid,
        slippage_paid=portfolio.slippage_paid,
        halted_reason=risk.halted_reason,
    )
    return BacktestResult(metrics=metrics, trades=portfolio.trades, engine=engine)


def run_from_config(candles: list[Candle], strategy: Strategy, config: Config) -> BacktestResult:
    return run(
        candles=candles,
        strategy=strategy,
        starting_cash=config.account.starting_cash,
        costs=config.costs,
        limits=config.risk,
        execution=config.execution,
    )
