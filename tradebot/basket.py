"""Run one strategy across several markets at once, and hold the result as a portfolio.

Everything else in this package trades a single instrument, and every conclusion it has
reached is therefore about a single instrument. That is a real limitation, because the
one improvement in finance that does not require predicting anything is refusing to put
everything in one place. Two assets that both drift upward but wobble out of step give
you the average of their returns and less than the average of their wobble - the only
free lunch on the menu, and this package could not order it.

What this does
--------------
Capital is split equally at the start and each market runs its own engine, its own
strategy instance and its own books, exactly as it would alone. The equity curves are
then summed. That is a real portfolio anyone could hold: buy a fifth of your money in
each of five things and let them run.

Nothing is rebalanced between markets. Rebalancing means selling a winner to buy a
loser, which costs money on both legs, and this package has spent its whole life
demonstrating that trading costs are what kill returns. A version that rebalances is
easy to add and would need to prove it earns back its own fees before it belonged here.

What to compare it against
--------------------------
Two benchmarks, and confusing them is the easy way to claim a victory you have not won:

* **Equal-weight buy-and-hold of the same basket.** This is the honest test of the
  *strategy*. Beating it means the rules added something.
* **Buy-and-hold of any single member.** Beating that may only mean diversification
  worked, which is a property of the basket rather than of anything the strategy did.

Both are reported, always, for exactly that reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import backtest as backtest_mod
from .costs import CostModel
from .engine import ExecutionSettings
from .metrics import Metrics, summarise
from .risk import RiskLimits
from .strategies import build
from .types import Candle, EquityPoint, Trade


@dataclass
class BasketResult:
    """A portfolio's combined result, plus the pieces it was built from."""

    symbols: list[str]
    metrics: Metrics
    benchmark: Metrics
    per_symbol: dict[str, Metrics] = field(default_factory=dict)
    curve: list[EquityPoint] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)

    @property
    def gap(self) -> float:
        """Percentage points against holding the same basket, equally weighted."""
        return self.metrics.total_return_pct - self.benchmark.total_return_pct

    @property
    def drawdown_cut(self) -> float:
        return self.benchmark.max_drawdown_pct - self.metrics.max_drawdown_pct


MS_PER_DAY = 86_400_000


def to_daily(candles: list[Candle]) -> list[Candle]:
    """Roll finer bars up into one bar per UTC day.

    A basket can only be measured on a shared clock, and markets do not share one:
    crypto trades around the clock in hourly bars here, stocks in daily ones stamped
    at an exchange open. Rolling everything to a UTC day is the coarsest common unit,
    and coarse is the right direction to err - a day's high is genuinely the highest
    price that day, whereas inventing hourly stock bars would invent prices.
    """
    buckets: dict[int, list] = {}
    for candle in candles:
        day = candle.ts // MS_PER_DAY
        bucket = buckets.get(day)
        if bucket is None:
            buckets[day] = [candle.open, candle.high, candle.low, candle.close, candle.volume]
        else:
            bucket[1] = max(bucket[1], candle.high)
            bucket[2] = min(bucket[2], candle.low)
            bucket[3] = candle.close
            bucket[4] += candle.volume
    return [
        Candle(ts=day * MS_PER_DAY, open=b[0], high=b[1], low=b[2], close=b[3], volume=b[4])
        for day, b in sorted(buckets.items())
    ]


def correlation(series: dict[str, list[Candle]]) -> dict[tuple[str, str], float]:
    """Pairwise correlation of daily returns - what decides whether a basket diversifies.

    Worth looking at before assuming a basket helps. Ten holdings that all move together
    are one holding with extra fees; this package learned that the expensive way, on a
    basket of ten US stocks whose worst drawdown came out *worse* than the single index
    it was meant to improve on.
    """
    aligned = align(series)
    rets = {
        name: [b.close / a.close - 1.0 for a, b in zip(bars, bars[1:]) if a.close > 0]
        for name, bars in aligned.items()
    }
    out: dict[tuple[str, str], float] = {}
    names = sorted(rets)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            x, y = rets[a], rets[b]
            n = min(len(x), len(y))
            x, y = x[:n], y[:n]
            if n < 2:
                out[(a, b)] = 0.0
                continue
            mx, my = sum(x) / n, sum(y) / n
            num = sum((i2 - mx) * (j2 - my) for i2, j2 in zip(x, y))
            den = (sum((i2 - mx) ** 2 for i2 in x) * sum((j2 - my) ** 2 for j2 in y)) ** 0.5
            out[(a, b)] = num / den if den else 0.0
    return out


def align(series: dict[str, list[Candle]]) -> dict[str, list[Candle]]:
    """Trim every market to the timestamps they all share.

    A basket has to be measured over one window. Left unaligned, a market with a later
    start silently contributes nothing to the early years while its capital is counted
    from day one, which reads as a drag that never happened.
    """
    if not series:
        raise ValueError("a basket needs at least one market")
    common = set.intersection(*(set(c.ts for c in bars) for bars in series.values()))
    if not common:
        raise ValueError("these markets share no common timestamps")
    return {name: [c for c in bars if c.ts in common] for name, bars in series.items()}


def _combine(curves: list[list[EquityPoint]]) -> list[EquityPoint]:
    """Sum the sleeves bar by bar into one portfolio curve."""
    length = min(len(c) for c in curves)
    out: list[EquityPoint] = []
    for i in range(length):
        first = curves[0][i]
        out.append(EquityPoint(
            ts=first.ts,
            equity=sum(c[i].equity for c in curves),
            price=first.price,
            position=sum(c[i].position for c in curves),
            cash=sum(c[i].cash for c in curves),
        ))
    return out


def run(
    series: dict[str, list[Candle]],
    strategy: str,
    params: dict | None = None,
    starting_cash: float = 10_000.0,
    costs: CostModel | None = None,
    limits: RiskLimits | None = None,
    execution: ExecutionSettings | None = None,
) -> BasketResult:
    """Split capital equally, run ``strategy`` on each market, and add up the result."""
    aligned = align(series)
    names = sorted(aligned)
    if not names:
        raise ValueError("a basket needs at least one market")

    params = params or {}
    per_sleeve = starting_cash / len(names)
    common = dict(starting_cash=per_sleeve, costs=costs, limits=limits, execution=execution)

    active_curves, bench_curves = [], []
    per_symbol: dict[str, Metrics] = {}
    trades: list[Trade] = []
    fees = slippage = 0.0
    bench_fees = bench_slippage = 0.0
    bench_trades: list[Trade] = []

    for name in names:
        bars = aligned[name]
        run_active = backtest_mod.run(bars, build(strategy, **params), **common)
        run_bench = backtest_mod.run(bars, build("buy_and_hold"), **common)

        per_symbol[name] = run_active.metrics
        active_curves.append(run_active.engine.portfolio.equity_curve)
        bench_curves.append(run_bench.engine.portfolio.equity_curve)

        trades += run_active.trades
        fees += run_active.engine.portfolio.fees_paid
        slippage += run_active.engine.portfolio.slippage_paid
        bench_trades += run_bench.trades
        bench_fees += run_bench.engine.portfolio.fees_paid
        bench_slippage += run_bench.engine.portfolio.slippage_paid

    curve = _combine(active_curves)
    bench_curve = _combine(bench_curves)

    return BasketResult(
        symbols=names,
        metrics=summarise(curve, trades, starting_cash, fees, slippage),
        benchmark=summarise(bench_curve, bench_trades, starting_cash, bench_fees, bench_slippage),
        per_symbol=per_symbol,
        curve=curve,
        trades=trades,
    )


def render(result: BasketResult, currency: str = "£") -> str:
    """The basket's result, next to the two things it has to be judged against."""
    m, b = result.metrics, result.benchmark
    single_best = max(result.per_symbol.items(), key=lambda kv: kv[1].total_return_pct)

    lines = [
        "",
        f"  Basket of {len(result.symbols)}: {', '.join(result.symbols)}",
        "  " + "-" * 62,
        f"  {'':<22}{'return':>10}{'worst fall':>13}{'Sharpe':>9}{'trades':>9}",
        f"  {'strategy, diversified':<22}{m.total_return_pct:>9.1f}%{m.max_drawdown_pct:>12.1f}%"
        f"{m.sharpe:>9.2f}{m.trades:>9,}",
        f"  {'hold the same basket':<22}{b.total_return_pct:>9.1f}%{b.max_drawdown_pct:>12.1f}%"
        f"{b.sharpe:>9.2f}{b.trades:>9,}",
        "",
        f"  vs holding the basket   {result.gap:+.1f}pp"
        f"   drawdown {result.drawdown_cut:+.1f} points",
        "",
    ]

    if result.gap > 0:
        lines.append("  The strategy beat holding the same basket. That is the comparison")
        lines.append("  that counts - it is not diversification wearing a strategy's name.")
    else:
        lines.append("  The strategy lost to simply holding the same basket. Any improvement")
        lines.append("  over a single market here came from diversifying, not from the rules.")

    lines += [
        "",
        f"  For scale, the best single member was {single_best[0]} at "
        f"{single_best[1].total_return_pct:+.1f}%.",
        "  Beating one member is not a result; a basket is meant to beat its own average,",
        "  and the row above is that average.",
        "",
    ]
    return "\n".join(lines)
