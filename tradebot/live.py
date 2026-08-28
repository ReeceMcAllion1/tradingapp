"""The unattended runner: warm up, then trade every bar until stopped.

This is the "fully automated" part. It polls for newly closed bars, runs each one
through the same engine the backtester uses, and persists its state after every bar
so a crash or a restart resumes rather than starting again from a wrong idea of what
it owns.

It runs in paper mode by default. Paper mode is not a toy: it is the identical code
path against identical live data, with the fills simulated. Run it for a few weeks
before considering anything else - if it does not make money on paper, it will not
make money with real money, and the only thing you will have lost is time.
"""

from __future__ import annotations

import json
import logging
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from .brokers.base import Broker
from .config import Config
from .engine import Engine
from .feeds.base import Feed
from .metrics import summarise
from .portfolio import Portfolio
from .risk import RiskManager
from .strategies.base import Context, Strategy
from .types import Candle

log = logging.getLogger("tradebot.live")


def configure_logging(log_file: str | None = None, verbose: bool = False) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


@dataclass
class LiveRunner:
    config: Config
    strategy: Strategy
    feed: Feed
    broker: Broker

    def __post_init__(self) -> None:
        self.portfolio = Portfolio(starting_cash=self.config.account.starting_cash)
        self.risk = RiskManager(limits=self.config.risk, costs=self.config.costs)
        self.engine = Engine(
            strategy=self.strategy,
            portfolio=self.portfolio,
            risk=self.risk,
            costs=self.config.costs,
            broker=self.broker,
            execution=self.config.execution,
        )
        self._stop = False
        self._bars = 0

    # ------------------------------------------------------------------ lifecycle

    def _install_signal_handlers(self) -> None:
        def handle(signum, _frame):
            log.warning("signal %s received - finishing this bar then stopping", signum)
            self._stop = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handle)
            except (ValueError, OSError):
                pass  # not on the main thread; the caller owns shutdown instead

    def warm_up(self) -> int:
        """Replay history through the strategy so its indicators are ready.

        Crucially this does *not* go through the engine, so no orders are generated
        from historical bars. Without it, a restarted bot would spend its first fifty
        bars blind and trade on half-formed indicators.
        """
        needed = max(self.strategy.warmup * 3, 100)
        history = self.feed.history(needed)
        flat = Context(exposure=0.0, equity=self.config.account.starting_cash, costs=self.config.costs)
        for candle in history:
            self.strategy.on_candle(candle, flat)
        log.info("warmed up on %d historical bars", len(history))
        return len(history)

    def run(self, max_bars: int | None = None) -> None:
        self._install_signal_handlers()
        self.load_state()
        self.warm_up()

        mode = "LIVE (real money)" if self.broker.is_live and not self.config.live.dry_run else "paper"
        log.info(
            "starting %s on %s %s | strategy=%s | cash=%.2f %s | position=%.8f",
            mode,
            self.config.market.symbol,
            self.config.market.interval,
            self.strategy.name,
            self.portfolio.cash,
            self.config.account.currency,
            self.portfolio.qty,
        )

        for candle in self.feed.stream():
            self.on_bar(candle)
            self._bars += 1
            if self._stop or (max_bars is not None and self._bars >= max_bars):
                break

        self.shutdown()

    def on_bar(self, candle: Candle) -> None:
        fills = self.engine.process(candle)
        equity = self.portfolio.equity(candle.close)

        if fills:
            for fill in fills:
                log.info(
                    "FILL %s %.8f @ %.2f (fee %.4f) - %s",
                    fill.side.value.upper(), fill.qty, fill.price, fill.fee, fill.reason,
                )
        log.info(
            "bar %s close=%.2f position=%.8f equity=%.2f %s",
            candle.ts, candle.close, self.portfolio.qty, equity, self.config.account.currency,
        )

        for event in self.risk.events[-3:]:
            log.warning("risk: %s", event)

        if self.risk.halted_reason:
            log.error("TRADING HALTED: %s", self.risk.halted_reason)

        self._maybe_reconcile()
        self.save_state()

    def _maybe_reconcile(self) -> None:
        """Periodically trust the exchange's position over our own bookkeeping."""
        every = self.config.live.reconcile_every_bars
        if not self.broker.is_live or every <= 0 or self._bars % every != 0:
            return
        actual = self.broker.sync_position()
        if actual is None:
            return
        if abs(actual - self.portfolio.qty) > 1e-8:
            log.error(
                "position mismatch: exchange says %.8f, we think %.8f - trusting the exchange",
                actual, self.portfolio.qty,
            )
            self.portfolio.qty = actual

    def shutdown(self) -> None:
        self.save_state()
        curve = self.portfolio.equity_curve
        if not curve:
            log.info("stopped before any bars were processed")
            return
        metrics = summarise(
            curve=curve,
            trades=self.portfolio.trades,
            starting_equity=self.config.account.starting_cash,
            fees_paid=self.portfolio.fees_paid,
            slippage_paid=self.portfolio.slippage_paid,
            halted_reason=self.risk.halted_reason,
        )
        print(metrics.render(f"Session summary ({self.config.market.symbol})"))
        if not self.portfolio.is_flat:
            log.warning(
                "stopped while still holding %.8f - the position stays open at the venue",
                self.portfolio.qty,
            )

    # ------------------------------------------------------------------ persistence

    @property
    def state_path(self) -> Path:
        return Path(self.config.live.state_file)

    def save_state(self) -> None:
        payload = {
            "saved_at": int(time.time() * 1000),
            "symbol": self.config.market.symbol,
            "interval": self.config.market.interval,
            "strategy": self.strategy.name,
            "engine": self.engine.state(),
        }
        path = self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write then move, so an interrupted save cannot leave a truncated state file
        # that would be unreadable on the next start.
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp.replace(path)

    def load_state(self) -> bool:
        path = self.state_path
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not read state file %s (%s) - starting fresh", path, exc)
            return False

        if payload.get("symbol") != self.config.market.symbol:
            log.warning(
                "state file is for %s but this run is %s - starting fresh",
                payload.get("symbol"), self.config.market.symbol,
            )
            return False

        self.engine.restore(payload["engine"])
        log.info(
            "resumed from %s: position %.8f, cash %.2f%s",
            path, self.portfolio.qty, self.portfolio.cash,
            f", HALTED ({self.risk.halted_reason})" if self.risk.halted_reason else "",
        )
        return True
