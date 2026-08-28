"""Walk-forward validation: would the choice have worked on data you had not seen?

Every result in this repository so far, including the ones I was pleased about, is
in-sample. The strategies were written after looking at the data, and the parameters
were picked by running a grid over the same bars they are then reported on. That is
not a prediction of anything. It is a description of the past with the answer in hand.

Walk-forward is the standard correction. Split the history into consecutive segments;
on each one, pick the parameters that did best; then measure that choice on the *next*
segment, which the selection never saw. Repeat, and collect only the out-of-sample
results.

The number that matters is the gap between the two. In-sample performance says how
well the grid could be fitted to history. Out-of-sample performance says what you would
actually have earned. When the first is large and the second is near zero, the
strategy has no edge and the grid was just memorising noise - which is the usual
outcome, and the reason this module exists.

One simplification, stated because it flatters nothing: each test segment is run
standalone, so the strategy spends its warmup bars inside it. With segments far longer
than any warmup the distortion is small, and it is identical for every parameter set,
so the comparison between them stays fair.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from . import backtest as backtest_mod
from .costs import CostModel
from .engine import ExecutionSettings
from .risk import RiskLimits
from .strategies import build
from .types import Candle


@dataclass
class Fold:
    symbol: str
    index: int
    chosen: dict
    in_sample_gap: float
    out_of_sample_gap: float
    out_of_sample_return: float
    benchmark_return: float
    out_of_sample_drawdown_cut: float


@dataclass
class WalkForward:
    strategy: str
    folds: list[Fold] = field(default_factory=list)

    @property
    def in_sample(self) -> list[float]:
        return [f.in_sample_gap for f in self.folds]

    @property
    def out_of_sample(self) -> list[float]:
        return [f.out_of_sample_gap for f in self.folds]


def _segments(candles: list[Candle], count: int) -> list[list[Candle]]:
    size = len(candles) // count
    return [candles[i * size : (i + 1) * size] for i in range(count)]


def run(
    series: dict[str, list[Candle]],
    strategy: str,
    grid: dict[str, list],
    folds: int = 6,
    starting_cash: float = 1000.0,
    costs: CostModel | None = None,
    limits: RiskLimits | None = None,
    execution: ExecutionSettings | None = None,
) -> WalkForward:
    import itertools

    costs = costs or CostModel()
    limits = limits or RiskLimits()
    execution = execution or ExecutionSettings()
    common = dict(starting_cash=starting_cash, costs=costs, limits=limits, execution=execution)

    names = list(grid)
    combos = [dict(zip(names, v)) for v in itertools.product(*(grid[n] for n in names))]

    result = WalkForward(strategy=strategy)
    for symbol, candles in series.items():
        chunks = _segments(candles, folds + 1)
        for i in range(folds):
            train, test = chunks[i], chunks[i + 1]
            if len(train) < 50 or len(test) < 50:
                continue

            train_bench = backtest_mod.run(
                candles=train, strategy=build("buy_and_hold"), **common
            ).metrics
            scored = []
            for params in combos:
                m = backtest_mod.run(candles=train, strategy=build(strategy, **params), **common).metrics
                scored.append((m.total_return_pct - train_bench.total_return_pct, params))
            best_gap, chosen = max(scored, key=lambda pair: pair[0])

            test_bench = backtest_mod.run(
                candles=test, strategy=build("buy_and_hold"), **common
            ).metrics
            tested = backtest_mod.run(
                candles=test, strategy=build(strategy, **chosen), **common
            ).metrics

            result.folds.append(Fold(
                symbol=symbol,
                index=i + 1,
                chosen=chosen,
                in_sample_gap=best_gap,
                out_of_sample_gap=tested.total_return_pct - test_bench.total_return_pct,
                out_of_sample_return=tested.total_return_pct,
                benchmark_return=test_bench.total_return_pct,
                out_of_sample_drawdown_cut=test_bench.max_drawdown_pct - tested.max_drawdown_pct,
            ))
    return result


def render(result: WalkForward) -> str:
    if not result.folds:
        return "\n  Not enough data to walk forward.\n"

    lines = [
        "",
        f"  {result.strategy}: walk-forward validation",
        "  Parameters are chosen on each segment, then measured on the NEXT one.",
        "",
        f"  {'symbol':<10} {'fold':>4} {'chosen':<26} {'picked':>9} {'actual':>9} {'vs hold':>9} {'DD cut':>8}",
        "  " + "-" * 82,
    ]
    for fold in result.folds:
        chosen = ", ".join(f"{k}={v}" for k, v in fold.chosen.items())
        lines.append(
            f"  {fold.symbol.split('_')[0]:<10} {fold.index:>4} {chosen:<26} "
            f"{fold.in_sample_gap:>+8.1f}p {fold.out_of_sample_return:>+8.1f}% "
            f"{fold.out_of_sample_gap:>+8.1f}p {fold.out_of_sample_drawdown_cut:>+7.1f}"
        )

    ins, oos = result.in_sample, result.out_of_sample
    wins = sum(1 for g in oos if g > 0)
    cuts = [f.out_of_sample_drawdown_cut for f in result.folds]

    lines += [
        "",
        "  What the selection was worth",
        "  " + "-" * 28,
        f"  in-sample, as picked      {statistics.mean(ins):>+8.1f} pp   what fitting the grid promised",
        f"  out-of-sample, actual     {statistics.mean(oos):>+8.1f} pp   what it delivered next",
        f"  lost to overfitting       {statistics.mean(ins) - statistics.mean(oos):>+8.1f} pp",
        "",
        f"  folds beating buy-and-hold  {wins} of {len(oos)}",
        f"  median out-of-sample      {statistics.median(oos):>+8.1f} pp",
        f"  drawdown cut, median      {statistics.median(cuts):>+8.1f} points",
    ]

    if statistics.mean(oos) <= 0:
        lines += [
            "",
            "  >> Out of sample the selection did not beat holding. The in-sample number",
            "     above measures how well a grid can be fitted to history, which is not a",
            "     forecast and not money. On this evidence the parameters carry no",
            "     predictive information at all.",
        ]
    elif statistics.mean(ins) > statistics.mean(oos) * 2:
        lines += [
            "",
            "  >> Most of the in-sample advantage did not survive. Some effect may be real,",
            "     but size it by the out-of-sample column and nothing else.",
        ]
    if statistics.median(cuts) > 0:
        lines += [
            "",
            f"  Drawdowns were still smaller out of sample (median {statistics.median(cuts):+.1f} points),",
            "  which is the one effect that has been consistent throughout this work.",
        ]
    return "\n".join(lines) + "\n"
