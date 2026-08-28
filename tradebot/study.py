"""Multi-symbol, multi-strategy historical study.

Runs every strategy against every symbol over the same bars, with the same costs, and
reports each one against buy-and-hold on that symbol.

That benchmark column is the whole point. A strategy's own return tells you almost
nothing on its own: 2016-2026 was one of the strongest bull markets on record, so
almost any long-biased rule made money, and "made money" is therefore not evidence of
skill. The only question that matters is whether it beat doing nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import backtest as backtest_mod
from .costs import CostModel
from .engine import ExecutionSettings
from .feeds.csv_feed import CsvFeed, write_csv
from .feeds.yahoo import YahooError, YahooFeed
from .metrics import Metrics
from .risk import RiskLimits
from .strategies import build
from .types import Candle

BENCHMARK = "buy_and_hold"


@dataclass
class Row:
    symbol: str
    strategy: str
    metrics: Metrics

    @property
    def is_benchmark(self) -> bool:
        return self.strategy == BENCHMARK


@dataclass
class Study:
    rows: list[Row] = field(default_factory=list)
    spans: dict[str, str] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)

    def benchmark_for(self, symbol: str) -> Metrics | None:
        for row in self.rows:
            if row.symbol == symbol and row.is_benchmark:
                return row.metrics
        return None

    def strategies(self) -> list[str]:
        seen: list[str] = []
        for row in self.rows:
            if row.strategy not in seen:
                seen.append(row.strategy)
        return seen

    def symbols(self) -> list[str]:
        seen: list[str] = []
        for row in self.rows:
            if row.symbol not in seen:
                seen.append(row.symbol)
        return seen


def load_symbol(
    symbol: str,
    years: int = 10,
    cache_dir: str | Path = "data",
    refresh: bool = False,
) -> list[Candle]:
    """Fetch daily bars, caching to CSV so repeat runs are fast and reproducible."""
    cache = Path(cache_dir) / f"{symbol.lower()}_{years}y_1d.csv"
    if cache.exists() and not refresh:
        return CsvFeed(cache).load()

    range_ = "max" if years > 10 else f"{years}y"
    candles = YahooFeed(symbol=symbol, interval="1d", range_=range_).load()
    write_csv(cache, candles)
    return candles


def load_files(paths: list[str | Path]) -> dict[str, list[Candle]]:
    """Load bar series from local CSV files, keyed by filename.

    Lets a study run over anything you already have - intraday crypto, a broker
    export, a market with no Yahoo ticker - rather than only what one vendor serves.
    The comparison logic does not care where the bars came from.
    """
    series: dict[str, list[Candle]] = {}
    for path in paths:
        target = Path(path)
        series[target.stem.upper()] = CsvFeed(target).load()
    return series


def run(
    symbols: list[str],
    strategy_names: list[str],
    years: int = 10,
    starting_cash: float = 10_000.0,
    costs: CostModel | None = None,
    limits: RiskLimits | None = None,
    execution: ExecutionSettings | None = None,
    cache_dir: str | Path = "data",
    refresh: bool = False,
    progress=None,
    series: dict[str, list[Candle]] | None = None,
) -> Study:
    """Backtest each strategy on each symbol. The benchmark is always included.

    ``series`` supplies bars directly, bypassing the download entirely - used when
    the study runs over local files.
    """
    costs = costs or CostModel()
    limits = limits or RiskLimits()
    execution = execution or ExecutionSettings()

    names = list(strategy_names)
    if BENCHMARK not in names:
        names.insert(0, BENCHMARK)

    study = Study()
    for symbol in (list(series) if series is not None else symbols):
        try:
            if series is not None:
                candles = series[symbol]
            else:
                candles = load_symbol(symbol, years=years, cache_dir=cache_dir, refresh=refresh)
        except (YahooError, ValueError, OSError, KeyError) as exc:
            study.failures[symbol] = str(exc)
            if progress:
                progress(f"  {symbol}: SKIPPED - {exc}")
            continue

        from .feeds.yahoo import describe_span

        study.spans[symbol] = describe_span(candles)
        if progress:
            progress(f"  {symbol}: {study.spans[symbol]}")

        for name in names:
            result = backtest_mod.run(
                candles=candles,
                strategy=build(name),
                starting_cash=starting_cash,
                costs=costs,
                limits=limits,
                execution=execution,
            )
            study.rows.append(Row(symbol=symbol, strategy=name, metrics=result.metrics))

    return study


# ---------------------------------------------------------------------- reporting


def render(study: Study, currency: str = "$") -> str:
    """A table per symbol, then an aggregate, then the verdict."""
    out: list[str] = []

    header = (
        f"  {'strategy':<16} {'final':>12} {'total':>9} {'CAGR':>8} "
        f"{'maxDD':>8} {'Sharpe':>7} {'trades':>7} {'costs':>10} {'cost/yr':>8}  vs hold"
    )

    for symbol in study.symbols():
        benchmark = study.benchmark_for(symbol)
        out.append("")
        out.append(f"  {symbol}   {study.spans.get(symbol, '')}")
        out.append("  " + "-" * (len(header) - 2))
        out.append(header)
        for row in study.rows:
            if row.symbol != symbol:
                continue
            m = row.metrics
            if row.is_benchmark or benchmark is None:
                verdict = "  (benchmark)" if row.is_benchmark else ""
            else:
                gap = m.total_return_pct - benchmark.total_return_pct
                verdict = f"  {gap:+9.1f}pp"
            halted = " HALTED" if m.halted_reason else ""
            out.append(
                f"  {row.strategy:<16} {currency}{m.ending_equity:>11,.0f} "
                f"{m.total_return_pct:>8.1f}% {m.cagr_pct:>7.2f}% "
                f"{m.max_drawdown_pct:>7.1f}% {m.sharpe:>7.2f} {m.trades:>7,} "
                f"{currency}{m.total_costs:>9,.0f} {m.cost_drag_annual_pct:>7.1f}%{verdict}{halted}"
            )

    out.append("")
    short = [r for r in study.rows if not r.metrics.can_annualise]
    if short:
        out.append(
            f"  Note: {len(short)} of {len(study.rows)} runs are too short to annualise; "
            "they are excluded from the CAGR and Sharpe averages."
        )
    out.append("  Averages across all symbols")
    out.append("  " + "-" * (len(header) - 2))
    out.append(header)

    for name in study.strategies():
        rows = [r for r in study.rows if r.strategy == name]
        if not rows:
            continue
        count = len(rows)
        avg_final = sum(r.metrics.ending_equity for r in rows) / count
        avg_total = sum(r.metrics.total_return_pct for r in rows) / count
        # CAGR and Sharpe are suppressed to zero on any series too short to annualise.
        # Averaging those zeros in would quietly drag the column toward nothing and
        # make a good strategy look mediocre, so short rows are left out of these two
        # and counted separately.
        annualisable = [r for r in rows if r.metrics.can_annualise]
        avg_cagr = (
            sum(r.metrics.cagr_pct for r in annualisable) / len(annualisable)
            if annualisable else 0.0
        )
        avg_sharpe = (
            sum(r.metrics.sharpe for r in annualisable) / len(annualisable)
            if annualisable else 0.0
        )
        avg_dd = sum(r.metrics.max_drawdown_pct for r in rows) / count
        total_trades = sum(r.metrics.trades for r in rows)
        avg_costs = sum(r.metrics.total_costs for r in rows) / count
        avg_drag = sum(r.metrics.cost_drag_annual_pct for r in rows) / count

        if name == BENCHMARK:
            verdict = "  (benchmark)"
        else:
            gaps = []
            for row in rows:
                bench = study.benchmark_for(row.symbol)
                if bench is not None:
                    gaps.append(row.metrics.total_return_pct - bench.total_return_pct)
            verdict = f"  {sum(gaps) / len(gaps):+9.1f}pp" if gaps else ""

        out.append(
            f"  {name:<16} {currency}{avg_final:>11,.0f} {avg_total:>8.1f}% "
            f"{avg_cagr:>7.2f}% {avg_dd:>7.1f}% {avg_sharpe:>7.2f} "
            f"{total_trades:>7,} {currency}{avg_costs:>9,.0f} {avg_drag:>7.1f}%{verdict}"
        )

    out.append("")
    out.append(_verdict(study))
    return "\n".join(out) + "\n"


def _verdict(study: Study) -> str:
    """State plainly how many strategy runs beat simply holding."""
    lines: list[str] = []
    contests = 0
    wins = 0
    for row in study.rows:
        if row.is_benchmark:
            continue
        bench = study.benchmark_for(row.symbol)
        if bench is None:
            continue
        contests += 1
        if row.metrics.total_return_pct > bench.total_return_pct:
            wins += 1

    if not contests:
        return "  No comparisons were possible."

    lines.append("  Verdict")
    lines.append("  -------")
    lines.append(f"  {wins} of {contests} strategy runs beat simply buying and holding the same stock.")

    beaten = [
        f"{r.strategy} on {r.symbol}"
        for r in study.rows
        if not r.is_benchmark
        and (b := study.benchmark_for(r.symbol)) is not None
        and r.metrics.total_return_pct > b.total_return_pct
    ]
    if beaten:
        lines.append(f"  Those were: {', '.join(beaten)}.")
        lines.append(
            "  Treat that as luck until it survives different symbols and a different\n"
            "  decade. With this many combinations tested, a few winners are expected\n"
            "  by chance alone."
        )
    else:
        lines.append(
            "  None did. That is the ordinary result, not a broken backtest: buying and\n"
            "  holding pays costs twice in a decade, never sits in cash while the market\n"
            "  rises, and cannot be stopped out at the bottom."
        )
    return "\n".join(lines)
