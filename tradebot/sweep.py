"""Parameter sweeps, for seeing how much of a result is real and how much is choice.

A single backtest tells you what one parameter setting did. It cannot tell you whether
that setting was special or whether you simply tried enough of them. Those are very
different claims, and the difference is most of what separates a finding from a story.

So this runs a strategy across a grid and reports the whole distribution: best, worst,
median, and how many settings beat the benchmark. Three numbers make it useful.

* **Spread** - best minus worst. A strategy whose result swings 100 points across
  reasonable parameters has not been measured, it has been sampled.
* **Share beating the benchmark** - if half the grid wins, the median setting is a
  coin flip and the winner is a draw from noise.
* **Monotonicity** - whether results trend with a parameter or scatter. A trend is
  evidence of a mechanism; scatter is evidence of luck.

Reporting the best cell of a grid as though it were the expected outcome is the single
most common way backtests mislead, including when the person doing it is sincere.
"""

from __future__ import annotations

import itertools
import statistics
from dataclasses import dataclass, field

from . import backtest as backtest_mod
from .costs import CostModel
from .engine import ExecutionSettings
from .metrics import Metrics
from .risk import RiskLimits
from .strategies import build
from .types import Candle


@dataclass
class Cell:
    params: dict
    symbol: str
    metrics: Metrics
    benchmark: Metrics

    @property
    def gap(self) -> float:
        """Percentage points of total return versus buy-and-hold on the same bars."""
        return self.metrics.total_return_pct - self.benchmark.total_return_pct

    @property
    def drawdown_cut(self) -> float:
        return self.benchmark.max_drawdown_pct - self.metrics.max_drawdown_pct


@dataclass
class Sweep:
    strategy: str
    cells: list[Cell] = field(default_factory=list)

    def settings(self) -> list[dict]:
        seen: list[dict] = []
        for cell in self.cells:
            if cell.params not in seen:
                seen.append(cell.params)
        return seen

    def mean_gap(self, params: dict) -> float:
        gaps = [c.gap for c in self.cells if c.params == params]
        return sum(gaps) / len(gaps) if gaps else 0.0

    def mean_drawdown_cut(self, params: dict) -> float:
        cuts = [c.drawdown_cut for c in self.cells if c.params == params]
        return sum(cuts) / len(cuts) if cuts else 0.0


def run(
    series: dict[str, list[Candle]],
    strategy: str,
    grid: dict[str, list],
    starting_cash: float = 1000.0,
    costs: CostModel | None = None,
    limits: RiskLimits | None = None,
    execution: ExecutionSettings | None = None,
) -> Sweep:
    costs = costs or CostModel()
    limits = limits or RiskLimits()
    execution = execution or ExecutionSettings()
    common = dict(starting_cash=starting_cash, costs=costs, limits=limits, execution=execution)

    benchmarks = {
        symbol: backtest_mod.run(candles=candles, strategy=build("buy_and_hold"), **common).metrics
        for symbol, candles in series.items()
    }

    sweep = Sweep(strategy=strategy)
    names = list(grid)
    for values in itertools.product(*(grid[n] for n in names)):
        params = dict(zip(names, values))
        for symbol, candles in series.items():
            metrics = backtest_mod.run(
                candles=candles, strategy=build(strategy, **params), **common
            ).metrics
            sweep.cells.append(
                Cell(params=params, symbol=symbol, metrics=metrics, benchmark=benchmarks[symbol])
            )
    return sweep


def render(sweep: Sweep) -> str:
    if not sweep.cells:
        return "\n  Nothing to sweep.\n"

    symbols = []
    for cell in sweep.cells:
        if cell.symbol not in symbols:
            symbols.append(cell.symbol)

    param_names = list(sweep.cells[0].params)
    header = (
        "  " + "".join(f"{n:>{max(8, len(n) + 2)}}" for n in param_names)
        + " |" + "".join(f"{s.split('_')[0]:>10}" for s in symbols)
        + f"{'mean':>10}{'DD cut':>9}"
    )
    lines = ["", f"  {sweep.strategy}: every setting, against buy-and-hold (percentage points)", "",
             header, "  " + "-" * (len(header) - 2)]

    means = []
    for params in sweep.settings():
        row = "  " + "".join(
            f"{params[n]:>{max(8, len(n) + 2)}}" if not isinstance(params[n], float)
            else f"{params[n]:>{max(8, len(n) + 2)}.3f}"
            for n in param_names
        ) + " |"
        for symbol in symbols:
            cell = next(c for c in sweep.cells if c.params == params and c.symbol == symbol)
            row += f"{cell.gap:>+10.1f}"
        mean = sweep.mean_gap(params)
        means.append(mean)
        row += f"{mean:>+10.1f}{sweep.mean_drawdown_cut(params):>+9.1f}"
        lines.append(row)

    best, worst = max(means), min(means)
    beat = sum(1 for m in means if m > 0)
    lines += [
        "",
        "  How much of this is real",
        "  " + "-" * 24,
        f"  settings tested       {len(means):>10}",
        f"  beat buy-and-hold     {beat:>10}  of {len(means)}",
        f"  best / worst          {best:>+10.1f} / {worst:+.1f} pp",
        f"  median                {statistics.median(means):>+10.1f} pp",
        f"  spread                {best - worst:>10.1f} pp",
    ]

    if best - worst > abs(statistics.median(means)) * 4:
        lines += [
            "",
            f"  >> The spread across settings ({best - worst:.0f}pp) dwarfs the median result.",
            "     Quoting the best cell here would be reporting a choice, not a finding.",
            "     Look for a trend across the grid instead - a mechanism moves results",
            "     smoothly, luck scatters them.",
        ]
    drawdown_cuts = [sweep.mean_drawdown_cut(p) for p in sweep.settings()]
    positive = sum(1 for d in drawdown_cuts if d > 0)
    lines += [
        "",
        f"  Drawdown was reduced in {positive} of {len(drawdown_cuts)} settings "
        f"(median {statistics.median(drawdown_cuts):+.1f} points).",
        "",
    ]
    return "\n".join(lines)
