"""Command line interface.

    python -m tradebot demo             what this whole thing is trying to tell you
    python -m tradebot strategies       list what is available
    python -m tradebot fetch            download history to CSV
    python -m tradebot backtest         test a strategy on history
    python -m tradebot paper            run automated, live data, simulated money
    python -m tradebot verify-keys      read-only check that API keys work
    python -m tradebot live             run automated with real money (heavily gated)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import backtest as backtest_mod
from . import config as config_mod
from .brokers import CryptoComBroker, PaperBroker
from .feeds import CryptoComFeed, CsvFeed, FeedError, SyntheticFeed, write_csv
from .live import LiveRunner, configure_logging
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


def _load_candles(args, config) -> list[Candle]:
    if getattr(args, "csv", None):
        return CsvFeed(args.csv).load()
    if getattr(args, "synthetic", False):
        return SyntheticFeed(bars=args.bars, seed=args.seed).generate()

    feed = CryptoComFeed(symbol=config.market.symbol, interval=config.market.interval)
    try:
        return feed.history(args.bars)
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


def cmd_fetch(args) -> int:
    config = config_mod.load(args.config)
    feed = CryptoComFeed(symbol=config.market.symbol, interval=config.market.interval)
    print(f"downloading {args.bars} {config.market.interval} bars of {config.market.symbol}...")
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

    if result.risk_events:
        print("  Risk events")
        print("  -----------")
        for event in result.risk_events[:10]:
            print(f"  {event}")
        print()

    if args.trades:
        print("  Trades")
        print("  ------")
        for trade in result.trades[: args.trades]:
            print(
                f"  {trade.side.value:<4} {trade.qty:>12.8f} "
                f"{trade.entry_price:>10.2f} -> {trade.exit_price:>10.2f}  "
                f"net {trade.net_pnl:>9.2f}  fees {trade.fees:>7.2f}  {trade.reason}"
            )
        print()

    print("  A backtest is the best case. Live results are worse, always.\n")
    return 0


def cmd_paper(args) -> int:
    config = config_mod.load(args.config)
    configure_logging(config.live.log_file, args.verbose)

    strategy = _build_strategy(args, config)
    _print_warnings(config, strategy)
    feed = CryptoComFeed(
        symbol=config.market.symbol,
        interval=config.market.interval,
        poll_seconds=config.live.poll_seconds,
    )
    broker = PaperBroker(config.costs)

    print(BANNER)
    print(f"  PAPER MODE - simulated money, live {config.market.symbol} data.")
    print(f"  Stop with Ctrl-C. State is saved to {config.live.state_file} after every bar.\n")

    runner = LiveRunner(config=config, strategy=strategy, feed=feed, broker=broker)
    runner.run(max_bars=args.max_bars)
    return 0


def cmd_verify_keys(args) -> int:
    config = config_mod.load(args.config)
    broker = CryptoComBroker(symbol=config.market.symbol, costs=config.costs)
    print("making one read-only balance request - no orders will be placed...")
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

    broker = CryptoComBroker(
        symbol=config.market.symbol,
        costs=config.costs,
        enabled=config.live.enabled,
        dry_run=config.live.dry_run,
        max_order_notional=config.live.max_order_notional,
        qty_decimals=config.live.qty_decimals,
    )

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
    feed = CryptoComFeed(
        symbol=config.market.symbol,
        interval=config.market.interval,
        poll_seconds=config.live.poll_seconds,
    )
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
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("paper", help="run automated on live data with simulated money")
    p.add_argument("-s", "--strategy", help="strategy name (default: from config)")
    p.add_argument("--max-bars", type=int, help="stop after N bars (default: run forever)")
    p.set_defaults(func=cmd_paper)

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
