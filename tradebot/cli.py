"""Command line interface.

    python -m tradebot demo             what this whole thing is trying to tell you
    python -m tradebot strategies       list what is available
    python -m tradebot fetch            download history to CSV
    python -m tradebot backtest         test a strategy on history
    python -m tradebot study            10 years of stocks vs buy-and-hold
    python -m tradebot trades           show every trade a strategy made
    python -m tradebot sweep            how much of a result is real vs cherry-picked
    python -m tradebot walkforward      would the choice have worked on unseen data?
    python -m tradebot paper            run automated, live data, simulated money
    python -m tradebot status           check on a run without stopping it
    python -m tradebot report           end-of-run verdict, benchmarked against holding
    python -m tradebot preflight        are you actually ready for real money?
    python -m tradebot verify-keys      read-only check that API keys work
    python -m tradebot live             run automated with real money (heavily gated)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import backtest as backtest_mod
from . import dashboard as dashboard_mod
from . import opportunity as opportunity_mod
from . import basket as basket_mod
from . import config as config_mod
from . import preflight as preflight_mod
from . import report as report_mod
from . import study as study_mod
from . import sweep as sweep_mod
from . import walkforward as walkforward_mod
from . import tradelog
from .brokers import AlpacaBroker, CryptoComBroker, PaperBroker
from .costs import CostModel
from .engine import ExecutionSettings
from .feeds import (
    AlpacaFeed,
    CryptoComFeed,
    CsvFeed,
    FeedError,
    SyntheticFeed,
    describe_span,
    write_csv,
)
from .live import LiveRunner, configure_logging
from .risk import RiskLimits
from .strategies import available, build
from .types import Candle

BANNER = """
  tradebot - an automated trading system

  Nothing here can promise you a profit. Markets are close to unpredictable and
  most retail algorithmic traders lose money. What this system does is trade a
  rule consistently, charge itself realistic costs, and refuse to risk more than
  you told it to. Whether the rule makes money is up to the market.

  Never run this with money you cannot afford to lose entirely.
"""


# ---------------------------------------------------------------------- helpers


def _market_feed(config, poll_seconds=None):
    """The data source this config points at: Alpaca when enabled, Crypto.com otherwise."""
    if config.alpaca.enabled:
        return AlpacaFeed(
            symbol=config.market.symbol,
            interval=config.market.interval,
            asset_class=config.alpaca.asset_class,
            data_feed=config.alpaca.data_feed,
            poll_seconds=poll_seconds,
        )
    return CryptoComFeed(
        symbol=config.market.symbol,
        interval=config.market.interval,
        poll_seconds=poll_seconds,
    )


def _load_candles(args, config) -> list[Candle]:
    if getattr(args, "csv", None):
        return CsvFeed(args.csv).load()
    if getattr(args, "synthetic", False):
        return SyntheticFeed(bars=args.bars, seed=args.seed).generate()

    try:
        return _market_feed(config).history(args.bars)
    except FeedError as exc:
        raise SystemExit(
            f"could not download market data: {exc}\n"
            "Use --csv to backtest a local file, or --synthetic for offline generated data."
        ) from exc


def _build_strategy(args, config):
    name = getattr(args, "strategy", None) or config.strategy.name
    params = dict(config.strategy.params) if name == config.strategy.name else {}
    try:
        return build(name, **params)
    except KeyError as exc:
        raise SystemExit(str(exc).strip("'")) from exc
    except TypeError as exc:
        raise SystemExit(f"bad parameters for strategy {name!r}: {exc}") from exc


def _print_warnings(config, strategy=None) -> None:
    warnings = list(config.validate())
    if strategy is not None:
        warnings += strategy.cost_warnings(config.costs)
    for warning in warnings:
        print(f"  ! {warning}", file=sys.stderr)


# ---------------------------------------------------------------------- commands


def cmd_strategies(args) -> int:
    print("\n  Available strategies\n  --------------------")
    for name, cls in sorted(available().items()):
        doc = (cls.__doc__ or "").strip().splitlines()
        print(f"  {name:<16} {doc[0] if doc else ''}")
    print()
    return 0


def cmd_demo(args) -> int:
    """Show, on real numbers, why "take any profit even a few pence" cannot work."""
    config = config_mod.load(args.config)
    costs = config.costs
    money = config.account.symbol

    print(BANNER)
    print("  The arithmetic of a tiny profit target")
    print("  " + "=" * 38)
    print(f"""
  Your costs per round trip, from config.toml:

    half spread     {costs.half_spread_bps:>6.1f} bp  x2 (in and out)
    slippage        {costs.slippage_bps:>6.1f} bp  x2
    commission      {costs.taker_fee_bps:>6.1f} bp  x2
    ------------------------------
    total           {costs.round_trip_bps:>6.1f} bp  = {costs.breakeven_move_pct():.3f}% per trade

  Price must move {costs.breakeven_move_pct():.3f}% in your favour before you make a single penny.

  On a {money}1,000 position that is {money}{costs.breakeven_cash(1000):.2f} of cost per trade.
  A "few pence" target of 5p therefore loses {money}{costs.breakeven_cash(1000) - 0.05:.2f} every time it wins.

  The profit target is a fixed amount while the cost is a percentage of position
  size, so the two only balance below one particular size:
""")
    for size in (10, 100, 1_000, 10_000):
        cost = costs.breakeven_cash(size)
        outcome = 0.05 - cost
        verdict = "clears costs" if outcome > 0 else "loses"
        print(
            f"    {money}{size:>7,} position -> 5p target, "
            f"{money}{cost:>8.2f} cost -> {money}{outcome:>8.2f} per trade  ({verdict})"
        )

    breakeven_size = 0.05 / (costs.round_trip_bps * 1e-4)
    print(f"""
  So the break-even position size for a 5p target is about {money}{breakeven_size:,.0f}.

  Below that it does technically work, and it is still not a business: you would
  tie up {money}{breakeven_size:,.0f} to earn 5p, needing a {0.05 / breakeven_size * 100:.2f}% move each time, and most
  venues reject orders that small. Twenty wins a day and not one loss is {money}1.

  What has no escape at all is a target set as a *percentage* smaller than the
  round trip - "sell as soon as I'm up a bit". Then both sides scale with position
  size, it cancels out, and you lose at every size. That is what micro_scalp does
  below, and what people usually mean by taking any profit.""")

    print("\n  Now the same point, run over price data:\n")

    candles = SyntheticFeed(bars=args.bars, seed=args.seed).generate()
    source = f"{len(candles):,} synthetic bars"
    if not args.offline:
        try:
            feed = CryptoComFeed(symbol=config.market.symbol, interval=config.market.interval)
            candles = feed.history(args.bars)
            source = f"{len(candles):,} real {config.market.interval} bars of {config.market.symbol}"
        except FeedError:
            print("  (no network - falling back to synthetic data; the arithmetic is the same)\n")

    print(f"  Data: {source}\n")

    for name in ("micro_scalp", "mean_reversion", "ema_cross"):
        strategy = build(name)
        result = backtest_mod.run_from_config(candles, strategy, config)
        print(result.metrics.render(f"{name} - {strategy.describe()}"))

    print("""
  Read those three tables side by side.

  micro_scalp wins most of its trades and still loses money, because each win is
  smaller than the cost of making it. The other two trade far less often and give
  themselves room to cover costs.

  That is the whole lesson. Any strategy here whose profit target sits below the
  round-trip cost will say so on every run, before it trades - so you find out
  from a warning rather than from your balance.

  Note that all three may well be losing money above. That is the honest result on
  this data, not a bug, and it is what most strategies do most of the time. None of
  them is a licence to print money. Test on your own data, on your own fees, then
  paper trade for weeks before you risk anything.
""")
    return 0


DEFAULT_STUDY_SYMBOLS = "SPY,AAPL,MSFT,JNJ,XOM,KO,GE,F,INTC,BA"


def cmd_study(args) -> int:
    """Backtest strategies over years of daily stock data, against buy-and-hold."""
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    names = [s.strip() for s in args.strategies.split(",") if s.strip()]

    costs = CostModel(
        taker_fee_bps=args.fee_bps,
        maker_fee_bps=args.fee_bps,
        half_spread_bps=args.spread_bps,
        slippage_bps=args.slippage_bps,
        flat_fee=args.flat_fee,
    )
    limits = RiskLimits(
        max_position_pct=args.max_position,
        max_daily_loss_pct=0.99,
        max_drawdown_pct=args.kill_switch,
        max_trades_per_day=1000,
        min_trade_notional=1.0,
        cooldown_bars_after_loss=0,
        allow_short=False,
    )

    print(BANNER)
    print(f"  Historical study: {args.years} years of daily bars, {len(symbols)} symbols")
    print(f"  Starting cash {args.cash:,.0f} per symbol, per strategy")
    print(
        f"  Costs: {args.fee_bps:.1f} bp commission, {args.spread_bps:.1f} bp half-spread, "
        f"{args.slippage_bps:.1f} bp slippage, {args.flat_fee:.2f} flat per trade"
    )
    print(f"  Round trip: {costs.breakeven_move_pct():.3f}% proportional", end="")
    if args.flat_fee > 0:
        example = args.cash * args.max_position
        print(f", {costs.breakeven_move_pct(example):.3f}% on a {example:,.0f} position")
    else:
        print()
    if args.kill_switch >= 0.99:
        print("  Drawdown kill switch: OFF (so strategies can be compared over the full period)")
    else:
        print(f"  Drawdown kill switch: {args.kill_switch:.0%}")
    print("\n  Loading data...")

    series = None
    if args.files:
        from glob import glob
        paths = sorted(set(sum((glob(pattern) for pattern in args.files), [])))
        if not paths:
            raise SystemExit(f"no files matched: {' '.join(args.files)}")
        series = study_mod.load_files(paths)
        symbols = list(series)

    result = study_mod.run(
        symbols=symbols,
        series=series,
        strategy_names=names,
        years=args.years,
        starting_cash=args.cash,
        costs=costs,
        limits=limits,
        execution=ExecutionSettings(min_notional=1.0),
        refresh=args.refresh,
        progress=print,
    )

    if not result.rows:
        raise SystemExit("no data could be loaded for any symbol")

    print(study_mod.render(result, currency="$"))

    # The study already compares each strategy to holding its own symbol. This asks the
    # other question - what the money would have done in an index fund instead - which
    # is the comparison a person actually faces when deciding whether to run any of it.
    if not args.no_index:
        active = [r for r in result.rows if not r.is_benchmark]
        best = max(active, key=lambda r: r.metrics.ending_equity, default=None)
        if best is not None:
            try:
                index = study_mod.load_symbol(opportunity_mod.DEFAULT_INDEX, years=args.years)
            except Exception as exc:  # noqa: BLE001 - a comparison must not lose the study
                print(f"  (no index comparison: {exc})", file=sys.stderr)
            else:
                print(opportunity_mod.render(
                    opportunity_mod.measure(index, args.cash),
                    best.metrics.ending_equity,
                    f"best strategy run ({best.strategy} on {best.symbol})", "$",
                ))

    if result.failures:
        print("  Symbols that failed to load:")
        for symbol, reason in result.failures.items():
            print(f"    {symbol}: {reason}")
        print()

    print("""  What this is and is not

  This is a backtest over one particular decade, on survivors you can name today.
  It is not evidence that any of these strategies will work next year. Read the
  "vs hold" column first and the return column second - in a bull market almost
  anything long makes money, so beating buy-and-hold is the only real test.
""")
    return 0


def cmd_trades(args) -> int:
    """Show every trade a strategy made, so the results can be checked."""
    config = config_mod.load(args.config)
    strategy = _build_strategy(args, config)
    money = config.account.symbol if args.csv or args.synthetic else "$"

    if args.symbol:
        try:
            candles = study_mod.load_symbol(args.symbol.upper(), years=args.years, refresh=args.refresh)
        except Exception as exc:  # noqa: BLE001 - the message matters more than the type
            raise SystemExit(f"could not load {args.symbol}: {exc}") from exc
        source = args.symbol.upper()
    else:
        candles = _load_candles(args, config)
        source = args.csv or (config.market.symbol if not args.synthetic else "synthetic data")

    costs = CostModel(
        taker_fee_bps=args.fee_bps, maker_fee_bps=args.fee_bps,
        half_spread_bps=args.spread_bps, slippage_bps=args.slippage_bps,
        flat_fee=args.flat_fee,
    )
    limits = RiskLimits(
        max_position_pct=args.max_position, max_daily_loss_pct=0.99,
        max_drawdown_pct=args.kill_switch, max_trades_per_day=1000,
        min_trade_notional=1.0, cooldown_bars_after_loss=0,
    )
    result = backtest_mod.run(
        candles=candles, strategy=strategy, starting_cash=args.cash,
        costs=costs, limits=limits, execution=ExecutionSettings(min_notional=1.0),
    )

    print(f"\n  {strategy.name} on {source} - {describe_span(candles)}")
    for warning in strategy.cost_warnings(costs):
        print(f"  ! {warning}", file=sys.stderr)

    limit = None if args.limit == 0 else args.limit
    print(tradelog.render(result.trades, starting_cash=args.cash, currency=money, limit=limit))

    if args.csv_out:
        path = tradelog.write_csv(args.csv_out, result.trades, starting_cash=args.cash)
        print(f"  wrote {len(result.trades):,} trades to {path}\n")
    return 0


def _print_opportunity(candles, ending_equity, starting_cash, label, currency="£"):
    """Price the do-nothing alternative over the same window, and say what it cost.

    Loaded lazily and failing quietly: this is a comparison, not the result, and a
    missing index file or a dead network must never take down someone's backtest.
    """
    try:
        index = study_mod.load_symbol(opportunity_mod.DEFAULT_INDEX, years=20)
    except Exception:
        return
    lo, hi = candles[0].ts, candles[-1].ts
    window = [c for c in index if lo <= c.ts <= hi]
    text = opportunity_mod.render(
        opportunity_mod.measure(window, starting_cash), ending_equity, label, currency
    )
    if text:
        print(text)


def cmd_sweep(args) -> int:
    """Run a strategy across a parameter grid and show the whole distribution."""
    result = sweep_mod.run(
        series=_series_from(args.files), strategy=args.strategy,
        grid=_grid_from(args.param), **_sweep_context(args),
    )
    print(sweep_mod.render(result))
    return 0


def _grid_from(specs: list[str]) -> dict[str, list]:
    grid: dict[str, list] = {}
    for spec in specs:
        if "=" not in spec:
            raise SystemExit(f"bad --param {spec!r}; expected name=v1,v2,v3")
        name, raw = spec.split("=", 1)
        values = []
        for token in raw.split(","):
            token = token.strip()
            values.append(float(token) if ("." in token or "e" in token.lower()) else int(token))
        grid[name.strip()] = values
    if not grid:
        raise SystemExit("give at least one --param name=v1,v2,v3")
    return grid


def cmd_basket(args) -> int:
    """Run one strategy across several markets at once, held as a portfolio."""
    series = _series_from(args.files)
    if args.daily:
        series = {name: basket_mod.to_daily(bars) for name, bars in series.items()}

    if args.correlations:
        pairs = basket_mod.correlation(series)
        print("\n  Daily return correlation - low numbers are what diversify")
        print("  " + "-" * 52)
        for (a, b), value in sorted(pairs.items(), key=lambda kv: kv[1]):
            print(f"  {a:>12} vs {b:<12} {value:>7.2f}")
        average = sum(pairs.values()) / len(pairs) if pairs else 0.0
        print(f"\n  average pairwise correlation: {average:.2f}")
        print("  Near 1.0 means these are one holding wearing several names, and a")
        print("  basket of them diversifies nothing while paying several sets of fees.\n")

    result = basket_mod.run(
        series, args.strategy, _params_from(args.param), **_sweep_context(args)
    )
    print(basket_mod.render(result, currency=""))
    return 0


def _params_from(specs: list[str]) -> dict:
    out: dict = {}
    for spec in specs or []:
        if "=" not in spec:
            raise SystemExit(f"parameter needs name=value, got {spec!r}")
        name, raw = spec.split("=", 1)
        try:
            out[name] = int(raw) if raw.isdigit() else float(raw)
        except ValueError:
            out[name] = raw
    return out


def expand_paths(patterns: list[str], what: str = "file") -> list[str]:
    """Turn arguments into real file paths, expanding any globs ourselves.

    Unix shells expand ``configs/*.toml`` before the program ever sees it. The Windows
    command prompt does not - it hands the asterisk over untouched - so a command copied
    from a README worked on one machine and died on another with a stack trace about a
    file literally named ``*.toml``. Expanding here means one documented command line
    behaves the same everywhere.

    Already-expanded arguments pass through untouched, so this is safe on both.
    """
    from glob import glob

    found: list[str] = []
    for pattern in patterns:
        matches = sorted(glob(pattern))
        if matches:
            found += [m for m in matches if Path(m).is_file()]
        elif Path(pattern).is_file():
            found.append(pattern)

    seen: list[str] = []
    for path in found:
        if path not in seen:
            seen.append(path)
    if not seen:
        raise SystemExit(
            f"no {what} matched: {' '.join(patterns)}\n"
            f"  Check you are in the project folder - on Windows, 'cd' to it first."
        )
    return seen


def _series_from(patterns: list[str]) -> dict:
    from glob import glob

    return study_mod.load_files(expand_paths(patterns, "bar file"))


def _sweep_context(args):
    return dict(
        starting_cash=args.cash,
        costs=CostModel(
            taker_fee_bps=args.fee_bps, maker_fee_bps=args.fee_bps,
            half_spread_bps=args.spread_bps, slippage_bps=args.slippage_bps,
        ),
        limits=RiskLimits(
            max_position_pct=1.0, max_daily_loss_pct=0.99, max_drawdown_pct=0.99,
            max_trades_per_day=10_000, min_trade_notional=1.0, cooldown_bars_after_loss=0,
        ),
        execution=ExecutionSettings(min_notional=1.0),
    )


def cmd_walkforward(args) -> int:
    """Pick parameters on past data, measure them on data the choice never saw."""
    result = walkforward_mod.run(
        series=_series_from(args.files), strategy=args.strategy,
        grid=_grid_from(args.param), folds=args.folds, **_sweep_context(args),
    )
    print(walkforward_mod.render(result))
    return 0


def cmd_fetch(args) -> int:
    config = config_mod.load(args.config)
    feed = _market_feed(config)
    venue = "Alpaca" if config.alpaca.enabled else "Crypto.com"
    print(f"downloading {args.bars} {config.market.interval} bars of {config.market.symbol} from {venue}...")
    try:
        candles = feed.history(args.bars)
    except FeedError as exc:
        raise SystemExit(f"download failed: {exc}") from exc
    path = write_csv(args.output, candles)
    span_days = (candles[-1].ts - candles[0].ts) / 86_400_000
    print(f"wrote {len(candles):,} bars ({span_days:.1f} days) to {path}")
    return 0


def cmd_backtest(args) -> int:
    config = config_mod.load(args.config)
    strategy = _build_strategy(args, config)
    _print_warnings(config, strategy)
    candles = _load_candles(args, config)

    span_days = (candles[-1].ts - candles[0].ts) / 86_400_000
    print(f"\n  {len(candles):,} bars spanning {span_days:.1f} days")
    print(f"  strategy: {strategy.name} - {strategy.describe()}")
    print(f"  round-trip cost: {config.costs.breakeven_move_pct():.3f}% per trade")

    result = backtest_mod.run_from_config(candles, strategy, config)
    print(result.metrics.render(f"Backtest: {strategy.name}"))
    if not args.no_index:
        _print_opportunity(candles, result.metrics.ending_equity,
                           config.account.starting_cash, strategy.name,
                           config.account.symbol)

    if result.risk_events:
        print("  Risk events")
        print("  -----------")
        for event in result.risk_events[:10]:
            print(f"  {event}")
        print()

    if args.trades:
        print(tradelog.render(
            result.trades,
            starting_cash=config.account.starting_cash,
            currency=config.account.symbol,
            limit=args.trades,
        ))

    if args.save_trades:
        path = tradelog.write_csv(
            args.save_trades, result.trades, starting_cash=config.account.starting_cash
        )
        print(f"  wrote {len(result.trades):,} trades to {path}\n")

    print("  A backtest is the best case. Live results are worse, always.\n")
    return 0


def _apply_market_overrides(config, args) -> None:
    """Let the command line override the market a config names.

    Editing a TOML file to try a different interval is a surprising amount of friction
    for a one-off, and on Windows it is worse than friction: hand-editing config files
    is exactly where a native path turns into invalid TOML. A flag avoids the file
    entirely.

    The state file is keyed by symbol, so overriding the symbol would try to resume one
    market's position into another's session. The runner already refuses that and starts
    fresh; this says so up front rather than leaving it to be discovered in a log.
    """
    if getattr(args, "interval", None):
        if args.interval != config.market.interval:
            print(f"  interval overridden: {config.market.interval} -> {args.interval}")
        config.market.interval = args.interval
    if getattr(args, "symbol", None):
        symbol = args.symbol.upper()
        if symbol != config.market.symbol:
            print(f"  symbol overridden: {config.market.symbol} -> {symbol}")
            print("  (a session's saved state belongs to its own symbol, so this starts fresh)")
        config.market.symbol = symbol


def cmd_paper(args) -> int:
    config = config_mod.load(args.config)
    _apply_market_overrides(config, args)
    configure_logging(config.live.log_file, args.verbose)

    strategy = _build_strategy(args, config)
    _print_warnings(config, strategy)
    feed = _market_feed(config, poll_seconds=config.live.poll_seconds)

    if config.alpaca.enabled:
        broker = AlpacaBroker(
            symbol=config.market.symbol,
            costs=config.costs,
            asset_class=config.alpaca.asset_class,
            paper=True,
            max_order_notional=config.live.max_order_notional,
            qty_decimals=config.live.qty_decimals,
        )
        source = f"Alpaca paper ({config.alpaca.asset_class})"
    else:
        broker = PaperBroker(config.costs)
        source = "simulated fills"

    print(BANNER)
    print(f"  PAPER MODE - {source}, live {config.market.symbol} data.")
    print(f"  Stop with Ctrl-C. State is saved to {config.live.state_file} after every bar.\n")

    runner = LiveRunner(config=config, strategy=strategy, feed=feed, broker=broker)
    runner.run(max_bars=args.max_bars)
    return 0


def cmd_preflight(args) -> int:
    """Report honestly whether this setup is ready to risk real money."""
    config = config_mod.load(args.config)
    verdict = None
    drag = None

    if not args.skip_backtest:
        try:
            strategy = _build_strategy(args, config)
            candles = study_mod.load_symbol(args.benchmark_symbol.upper(), years=args.years)
            common = dict(
                candles=candles, starting_cash=10_000.0, costs=config.costs,
                limits=RiskLimits(
                    max_position_pct=1.0, max_daily_loss_pct=0.99, max_drawdown_pct=0.99,
                    max_trades_per_day=1000, min_trade_notional=1.0, cooldown_bars_after_loss=0,
                ),
                execution=ExecutionSettings(min_notional=1.0),
            )
            active = backtest_mod.run(strategy=strategy, **common).metrics
            passive = backtest_mod.run(strategy=build("buy_and_hold"), **common).metrics
            drag = active.cost_drag_annual_pct
            gap = active.total_return_pct - passive.total_return_pct
            detail = (
                f"{strategy.name} returned {active.total_return_pct:+.1f}% vs "
                f"{passive.total_return_pct:+.1f}% for holding {args.benchmark_symbol.upper()} "
                f"over {active.years:.0f} years ({gap:+.1f}pp). "
            )
            if gap > 0:
                # In-sample, so this is the optimistic reading by construction. Walking
                # this package's own best candidate forward gave up 80% of its apparent
                # edge; a check that stayed quiet about that would be selling the same
                # illusion the rest of the tool exists to puncture.
                detail += (
                    "That is IN-SAMPLE and therefore the best case: the strategy and its "
                    "parameters were chosen knowing this data. Confirm it with "
                    "`tradebot walkforward` before believing it."
                )
            else:
                detail += (
                    "Holding won, and this is the optimistic in-sample reading. Trading "
                    "this strategy would have cost you money you would have had by "
                    "doing nothing."
                )
            verdict = (gap > 0, detail)
        except Exception as exc:  # noqa: BLE001 - a failed check must not hide the rest
            print(f"  (could not run the benchmark backtest: {exc})", file=sys.stderr)

    checks = preflight_mod.run(config, backtest_verdict=verdict, annual_cost_drag_pct=drag)
    print(preflight_mod.render(checks))
    return 1 if any(not c.passed and c.blocking for c in checks) else 0


def cmd_status(args) -> int:
    """Report what a running (or stopped) session is doing, without disturbing it.

    Reads only the files the runner writes, so it is safe to call at any time against
    a live bot. Recommending a month of paper trading is hollow without a way to look
    in on it.
    """
    import json
    from datetime import datetime, timezone

    config = config_mod.load(args.config)
    state_path = Path(config.live.state_file)
    if not state_path.exists():
        raise SystemExit(f"no session state at {state_path}. Has this config ever run?")

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"could not read {state_path}: {exc}") from exc

    engine = payload.get("engine", {})
    book = engine.get("portfolio", {})
    risk = engine.get("risk", {})
    saved = payload.get("saved_at", 0)
    age = (datetime.now(tz=timezone.utc) - datetime.fromtimestamp(saved / 1000, tz=timezone.utc))
    money = config.account.symbol

    qty = float(book.get("qty", 0.0))
    cash = float(book.get("cash", 0.0))
    started = config.account.starting_cash

    print(f"\n  {payload.get('strategy', '?')} on {payload.get('symbol', '?')} "
          f"{payload.get('interval', '')}")
    print("  " + "-" * 52)
    print(f"  last update       {age.total_seconds() / 60:>10,.0f} min ago"
          + ("   <-- stale, is it still running?" if age.total_seconds() > 3600 else ""))
    print(f"  bars processed    {engine.get('bars_seen', 0):>10,}")
    print(f"  position          {qty:>10.8f}")
    print(f"  cash              {money}{cash:>9,.2f}")
    if qty:
        print(f"  average price     {float(book.get('avg_price', 0.0)):>10,.2f}")
    print(f"  costs paid        {money}{float(book.get('fees_paid', 0.0)) + float(book.get('slippage_paid', 0.0)):>9,.2f}"
          f"   of {money}{started:,.2f} started with")

    if risk.get("halted_reason"):
        print(f"\n  HALTED: {risk['halted_reason']}")
    if engine.get("consecutive_rejections"):
        print(f"  order rejections in a row: {engine['consecutive_rejections']}")

    trades_path = Path(config.live.trades_file) if config.live.trades_file else None
    if trades_path and trades_path.exists():
        import csv as csvmod

        with trades_path.open(newline="", encoding="utf-8") as handle:
            rows = list(csvmod.DictReader(handle))
        if rows:
            net = sum(float(r.get("net", 0) or 0) for r in rows)
            wins = sum(1 for r in rows if float(r.get("net", 0) or 0) > 0)
            print(f"\n  {len(rows)} closed trades, {wins} winners "
                  f"({wins / len(rows) * 100:.0f}%), net {money}{net:+,.2f}")
            print(f"  full log: {trades_path}")
            for row in rows[-args.recent:]:
                print(f"    {row['closed']}  {row['side']:<4} net {float(row['net']):>+8.2f}  {row['reason'][:40]}")
    print()
    return 0


def cmd_report(args) -> int:
    """Summarise finished or running sessions, against what holding would have done."""
    configs = expand_paths(args.configs, "config") if args.configs else (
        [args.config] if args.config else [])
    if not configs:
        raise SystemExit("give one or more config files, e.g. report state/*.toml")

    reports = []
    for path in configs:
        cfg = config_mod.load(path)
        try:
            reports.append(report_mod.load(cfg, name=Path(path).stem))
        except (FileNotFoundError, ValueError) as exc:
            print(f"  skipping {path}: {exc}", file=sys.stderr)

    if not reports:
        raise SystemExit("no readable sessions")

    # Mark open positions to market and work out what holding would have returned over
    # the very same window, which is the only comparison that means anything.
    if not args.offline:
        for r in reports:
            try:
                feed = CryptoComFeed(symbol=r.symbol, interval=r.interval or "1m")
                # Select the benchmark window by the session's real clock, not by how
                # many bars it processed. A session that stalled - a suspended machine,
                # a dropped feed - has fewer bars than elapsed minutes, and sizing the
                # window by bar count would then compare a days-old position against a
                # few minutes of market. Ask for enough bars to cover the elapsed time.
                needed = max(int(r.days * 24 * 60 / max(feed.interval_ms / 60000, 1)) + 10, 50)
                bars = feed.history(min(needed, 20_000))
                start_ms = r.started.timestamp() * 1000 if r.started else 0
                window = [c for c in bars if c.ts >= start_ms] or bars[-2:]
                if len(window) >= 2:
                    r.benchmark_return_pct = (window[-1].close / window[0].open - 1.0) * 100.0
                    report_mod.mark_to_market(r, window[-1].close)
            except Exception as exc:  # noqa: BLE001 - a missing benchmark must not lose the report
                print(f"  (no benchmark for {r.name}: {exc})", file=sys.stderr)

    currency = config_mod.load(configs[0]).account.symbol
    print(report_mod.render(reports, currency=currency))

    # And what the same money would have done doing nothing at all. Sessions are
    # separate sleeves of one pot, so they are priced together - the question is what
    # the whole allocation was worth, not each piece of it.
    if not args.offline and not args.no_index:
        combined = sum(r.equity for r in reports)
        staked = sum(r.starting_cash for r in reports)
        span = max((r for r in reports), key=lambda r: r.days, default=None)
        if span is not None and span.started is not None:
            try:
                index = study_mod.load_symbol(opportunity_mod.DEFAULT_INDEX, years=20)
                lo = span.started.timestamp() * 1000
                hi = span.updated.timestamp() * 1000 if span.updated else index[-1].ts
                window = [c for c in index if lo <= c.ts <= hi]
                label = "these sessions" if len(reports) > 1 else reports[0].name
                text = opportunity_mod.render(
                    opportunity_mod.measure(window, staked), combined, label, currency
                )
                if text:
                    print(text)
            except Exception as exc:  # noqa: BLE001 - a comparison must not lose the report
                print(f"  (no index comparison: {exc})", file=sys.stderr)
    return 0


def _position_of(report) -> float:
    return getattr(report, "_qty", 0.0)


def cmd_dashboard(args) -> int:
    """Serve a local page showing what every session is doing, refreshing itself."""
    import webbrowser

    configs = expand_paths(args.configs, "config") if args.configs else (
        [args.config] if args.config else [])
    if not configs:
        raise SystemExit("give one or more config files, e.g. dashboard configs/*.toml")

    sessions = []
    for path in configs:
        cfg = config_mod.load(path)
        sessions.append(dashboard_mod.Session(
            name=Path(path).stem,
            state_file=Path(cfg.live.state_file),
            trades_file=Path(cfg.live.trades_file),
            starting_cash=cfg.account.starting_cash,
            currency=cfg.account.symbol,
            symbol=cfg.market.symbol,
            interval=cfg.market.interval,
        ))

    try:
        server = dashboard_mod.serve(sessions, port=args.port)
    except OSError as exc:
        raise SystemExit(
            f"could not listen on {dashboard_mod.HOST}:{args.port} ({exc}). "
            "Something else is probably using that port - try --port 8766."
        ) from exc

    url = f"http://{dashboard_mod.HOST}:{args.port}"
    print(f"\n  Watching {len(sessions)} session(s). Open {url}")
    print("  Bound to localhost only - nothing outside this machine can reach it.")
    print("  Ctrl-C to stop. The bot keeps running either way.\n")
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - a headless box has no browser, and that is fine
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("  dashboard stopped\n")
    finally:
        server.server_close()
    return 0


def _live_broker(config):
    """The real-money broker for this config."""
    if config.alpaca.enabled:
        return AlpacaBroker(
            symbol=config.market.symbol,
            costs=config.costs,
            asset_class=config.alpaca.asset_class,
            paper=config.alpaca.paper,
            enabled=config.live.enabled,
            dry_run=config.live.dry_run,
            max_order_notional=config.live.max_order_notional,
            qty_decimals=config.live.qty_decimals,
        )
    return CryptoComBroker(
        symbol=config.market.symbol,
        costs=config.costs,
        enabled=config.live.enabled,
        dry_run=config.live.dry_run,
        max_order_notional=config.live.max_order_notional,
        qty_decimals=config.live.qty_decimals,
    )


def cmd_verify_keys(args) -> int:
    config = config_mod.load(args.config)
    if config.alpaca.enabled:
        broker = AlpacaBroker(
            symbol=config.market.symbol, costs=config.costs,
            asset_class=config.alpaca.asset_class, paper=config.alpaca.paper,
        )
    else:
        broker = CryptoComBroker(symbol=config.market.symbol, costs=config.costs)
    print("making one read-only account request - no orders will be placed...")
    try:
        print(f"  {broker.verify()}")
    except Exception as exc:  # noqa: BLE001 - the message matters more than the type
        print(f"  failed: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_live(args) -> int:
    config = config_mod.load(args.config)
    configure_logging(config.live.log_file, args.verbose)

    if not config.live.enabled:
        raise SystemExit(
            "live trading is disabled. Set [live] enabled = true in config.toml first.\n"
            "Before you do: run a backtest, then paper trade for several weeks."
        )
    if not args.yes_really_trade_live:
        raise SystemExit(
            "refusing to trade real money without --yes-really-trade-live on the command line.\n"
            "This flag is required on every run, on purpose."
        )

    broker = _live_broker(config)

    print(BANNER)
    if config.live.dry_run:
        print("  DRY RUN - orders will be logged in full but not sent.")
        print("  Set [live] dry_run = false in config.toml to send them for real.\n")
    else:
        print("  *** LIVE TRADING - REAL MONEY ***")
        print(f"  symbol {config.market.symbol}   max order {config.live.max_order_notional:.2f}")
        print(f"  daily loss limit {config.risk.max_daily_loss_pct:.1%}   "
              f"kill switch at {config.risk.max_drawdown_pct:.1%} drawdown\n")
        try:
            print(f"  {broker.verify()}\n")
        except Exception as exc:  # noqa: BLE001
            raise SystemExit(f"credential check failed, refusing to start: {exc}") from exc
        if input("  Type TRADE to confirm: ").strip() != "TRADE":
            print("  cancelled.")
            return 1

    strategy = _build_strategy(args, config)
    _print_warnings(config, strategy)
    feed = _market_feed(config, poll_seconds=config.live.poll_seconds)
    runner = LiveRunner(config=config, strategy=strategy, feed=feed, broker=broker)
    runner.run(max_bars=args.max_bars)
    return 0


def cmd_init_config(args) -> int:
    target = Path(args.output)
    if target.exists() and not args.force:
        raise SystemExit(f"{target} already exists; pass --force to overwrite")
    template = Path(__file__).resolve().parent.parent / "config.example.toml"
    if not template.exists():
        raise SystemExit(
            f"cannot find the template at {template}. Run this from a checkout of the "
            "repository, or copy config.example.toml by hand."
        )
    target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"wrote {target} - edit it, especially the [costs] section, to match your venue")
    return 0


# ---------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tradebot",
        description="An automated trading system that is honest about costs.",
    )
    parser.add_argument("--config", help="path to config.toml (defaults to ./config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("demo", help="show why a few-pence profit target loses money")
    p.add_argument("--bars", type=int, default=3000)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--offline", action="store_true", help="skip the network, use synthetic data")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("strategies", help="list available strategies")
    p.set_defaults(func=cmd_strategies)

    p = sub.add_parser("study", help="backtest strategies over years of stock data vs buy-and-hold")
    p.add_argument("--symbols", default=DEFAULT_STUDY_SYMBOLS, help="comma-separated tickers")
    p.add_argument("--files", nargs="+", metavar="CSV",
                   help="study local bar files instead of downloading (globs allowed)")
    p.add_argument(
        "--strategies", default="ema_cross,mean_reversion,micro_scalp",
        help="comma-separated strategy names (buy_and_hold is always added)",
    )
    p.add_argument("--years", type=int, default=10)
    p.add_argument("--cash", type=float, default=10_000.0, help="starting cash per run")
    p.add_argument("--max-position", type=float, default=1.0, help="max fraction of equity per position")
    p.add_argument(
        "--kill-switch", type=float, default=0.99, metavar="PCT",
        help="halt permanently at this drawdown (default 0.99 = effectively off)",
    )
    p.add_argument("--fee-bps", type=float, default=2.0, help="commission in basis points")
    p.add_argument("--spread-bps", type=float, default=1.0, help="half-spread in basis points")
    p.add_argument("--slippage-bps", type=float, default=2.0)
    p.add_argument("--flat-fee", type=float, default=0.0, help="flat commission per trade")
    p.add_argument("--refresh", action="store_true", help="re-download instead of using the cache")
    p.add_argument("--no-index", action="store_true",
                   help="skip the comparison against holding an index fund")
    p.set_defaults(func=cmd_study)

    p = sub.add_parser("trades", help="show every trade a strategy made")
    p.add_argument("-s", "--strategy", help="strategy name (default: from config)")
    p.add_argument("--symbol", help="stock ticker to download, e.g. SPY")
    p.add_argument("--csv", help="use a local CSV of bars instead")
    p.add_argument("--synthetic", action="store_true", help="use generated data, no network")
    p.add_argument("--years", type=int, default=10)
    p.add_argument("--bars", type=int, default=3000)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--cash", type=float, default=10_000.0)
    p.add_argument("--limit", type=int, default=40, metavar="N",
                   help="show the first N trades (0 for all). Default 40.")
    p.add_argument("--csv-out", metavar="PATH", help="also export the full log to this CSV")
    p.add_argument("--max-position", type=float, default=1.0)
    p.add_argument("--kill-switch", type=float, default=0.99)
    p.add_argument("--fee-bps", type=float, default=2.0)
    p.add_argument("--spread-bps", type=float, default=1.0)
    p.add_argument("--slippage-bps", type=float, default=2.0)
    p.add_argument("--flat-fee", type=float, default=0.0)
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_trades)

    p = sub.add_parser("sweep", help="run a parameter grid and show the full distribution")
    p.add_argument("-s", "--strategy", required=True)
    p.add_argument("--files", nargs="+", required=True, metavar="CSV", help="bar files (globs allowed)")
    p.add_argument("--param", nargs="+", required=True, metavar="NAME=V1,V2",
                   help="parameter grid, e.g. --param period=100,200,400 band_pct=0.0,0.02")
    p.add_argument("--cash", type=float, default=1000.0)
    p.add_argument("--fee-bps", type=float, default=7.5)
    p.add_argument("--spread-bps", type=float, default=1.0)
    p.add_argument("--slippage-bps", type=float, default=2.0)
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("basket", help="run one strategy across several markets as a portfolio")
    p.add_argument("--files", nargs="+", metavar="CSV", required=True,
                   help="bar files, globs allowed - one per market")
    p.add_argument("--strategy", default="vol_target")
    p.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    p.add_argument("--daily", action="store_true",
                   help="roll finer bars up to one a day, so markets on different clocks line up")
    p.add_argument("--correlations", action="store_true",
                   help="show what actually correlates before trusting the basket")
    p.add_argument("--cash", type=float, default=10_000.0, help="total capital, split equally")
    p.add_argument("--fee-bps", type=float, default=7.5, help="commission in basis points")
    p.add_argument("--spread-bps", type=float, default=1.0, help="half-spread in basis points")
    p.add_argument("--slippage-bps", type=float, default=2.0)
    p.set_defaults(func=cmd_basket)

    p = sub.add_parser("walkforward", help="choose parameters on past data, test on unseen data")
    p.add_argument("-s", "--strategy", required=True)
    p.add_argument("--files", nargs="+", required=True, metavar="CSV")
    p.add_argument("--param", nargs="+", required=True, metavar="NAME=V1,V2")
    p.add_argument("--folds", type=int, default=6)
    p.add_argument("--cash", type=float, default=1000.0)
    p.add_argument("--fee-bps", type=float, default=7.5)
    p.add_argument("--spread-bps", type=float, default=1.0)
    p.add_argument("--slippage-bps", type=float, default=2.0)
    p.set_defaults(func=cmd_walkforward)

    p = sub.add_parser("fetch", help="download historical bars to CSV")
    p.add_argument("--bars", type=int, default=5000)
    p.add_argument("-o", "--output", default="data/history.csv")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("backtest", help="replay a strategy over history")
    p.add_argument("-s", "--strategy", help="strategy name (default: from config)")
    p.add_argument("--csv", help="backtest a local CSV instead of downloading")
    p.add_argument("--synthetic", action="store_true", help="use generated data, no network")
    p.add_argument("--bars", type=int, default=3000)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--trades", type=int, default=0, metavar="N", help="print the first N trades")
    p.add_argument("--save-trades", metavar="PATH", help="export the full trade log to this CSV")
    p.add_argument("--no-index", action="store_true",
                   help="skip the comparison against holding an index fund")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("paper", help="run automated on live data with simulated money")
    p.add_argument("-s", "--strategy", help="strategy name (default: from config)")
    p.add_argument("--max-bars", type=int, help="stop after N bars (default: run forever)")
    p.add_argument("--interval", help="override the config's bar size, e.g. 1m 5m 1h")
    p.add_argument("--symbol", help="override the config's instrument, e.g. ETH_USD")
    p.set_defaults(func=cmd_paper)

    p = sub.add_parser("preflight", help="check whether you are ready to trade real money")
    p.add_argument("-s", "--strategy", help="strategy to evaluate (default: from config)")
    p.add_argument("--benchmark-symbol", default="SPY", help="symbol to test against buy-and-hold")
    p.add_argument("--years", type=int, default=10)
    p.add_argument("--skip-backtest", action="store_true", help="skip the benchmark comparison")
    p.set_defaults(func=cmd_preflight)

    p = sub.add_parser("report", help="end-of-run verdict for one or more sessions")
    p.add_argument("configs", nargs="*", metavar="CONFIG", help="session config files")
    p.add_argument("--offline", action="store_true", help="skip the buy-and-hold benchmark")
    p.add_argument("--no-index", action="store_true",
                   help="skip the comparison against holding an index fund")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("dashboard", help="watch live sessions in a browser page that refreshes itself")
    p.add_argument("configs", nargs="*", help="config files to watch (globs allowed)")
    p.add_argument("--port", type=int, default=dashboard_mod.DEFAULT_PORT)
    p.add_argument("--no-open", action="store_true", help="do not open a browser window")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("status", help="check on a running session without stopping it")
    p.add_argument("--recent", type=int, default=5, metavar="N", help="show the last N trades")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("verify-keys", help="read-only check that API credentials work")
    p.set_defaults(func=cmd_verify_keys)

    p = sub.add_parser("live", help="run automated with REAL MONEY (multiple gates required)")
    p.add_argument("-s", "--strategy", help="strategy name (default: from config)")
    p.add_argument("--max-bars", type=int)
    p.add_argument(
        "--yes-really-trade-live",
        action="store_true",
        help="required on every live run, in addition to config.toml gates",
    )
    p.set_defaults(func=cmd_live)

    p = sub.add_parser("init-config", help="write a starter config.toml")
    p.add_argument("-o", "--output", default="config.toml")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "verbose", False):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
