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
from . import tradelog
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
        self._logged_trades = 0
        self._started_at = int(time.time() * 1000)
        self._last_bar_ts = 0

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

        # Remember where history ended. A live stream polls for the newest *closed*
        # bar, and on a fresh stream that is the same bar warm-up just finished on, so
        # without this every start feeds one bar through the strategy twice and
        # advances its indicators an extra step.
        if history:
            self._last_bar_ts = max(self._last_bar_ts, history[-1].ts)

        # Tell the engine the strategy is already primed. The engine refuses to act on
        # its first ``warmup`` bars so nothing trades on half-formed indicators - but it
        # counts bars *it* has seen, and warm-up deliberately bypasses it. Left alone the
        # warm-up is therefore charged twice: the strategy is ready, and the engine sits
        # on its hands for another full warmup period of live bars.
        #
        # The cost of that scales with the bar size, which is what makes it serious
        # rather than untidy. A 30-bar strategy on one-minute candles loses half an hour.
        # A 200-day moving average on daily candles loses two hundred trading days -
        # the better part of a year in which the bot holds nothing, learns nothing and
        # reports nothing wrong - and the supervised runner restarts on crash, so it
        # pays that again every time.
        #
        # max() rather than assignment: a resumed session may already have seen more
        # bars than this warm-up covered, and a short history must still leave the
        # engine gating the difference.
        self.engine.bars_seen = max(self.engine.bars_seen, len(history))
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
            # Only bars that were actually processed count. Warm-up usually ends on the
            # newest closed bar and the stream then offers it again, so counting every
            # bar the feed hands over made "--max-bars 1" spend its whole budget on the
            # one bar the runner is right to ignore, and stop having done nothing.
            if self.on_bar(candle):
                self._bars += 1
            if self._stop or (max_bars is not None and self._bars >= max_bars):
                break

        self.shutdown()

    def on_bar(self, candle: Candle) -> bool:
        # Bars must arrive strictly newer, once each. A feed can repeat one after a
        # reconnect or a paging overlap, and warm-up has usually already shown the
        # strategy the newest closed bar. Replaying it advances every indicator an
        # extra step on a bar that only happened once, which silently shifts every
        # signal that follows. An older bar is worse still: it would re-open a risk
        # day that has already closed.
        if candle.ts <= self._last_bar_ts:
            log.debug("ignoring bar %s: not newer than %s", candle.ts, self._last_bar_ts)
            return False
        self._last_bar_ts = candle.ts

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

        self._record_closed_trades(candle.close)

        for event in self.risk.events[-3:]:
            log.warning("risk: %s", event)

        if self.risk.halted_reason:
            log.error("TRADING HALTED: %s", self.risk.halted_reason)

        self._maybe_reconcile()
        self.save_state()
        return True

    def _record_closed_trades(self, price: float) -> None:
        """Append any newly closed round trips to the trade log.

        Written as they happen rather than at shutdown, so an unattended run leaves a
        readable history even if it is killed, and so the file can be tailed while the
        bot is still going.
        """
        path = self.config.live.trades_file
        if not path:
            return
        new = self.portfolio.trades[self._logged_trades:]
        for offset, trade in enumerate(new):
            try:
                tradelog.append(
                    path, trade,
                    balance=self.portfolio.equity(price),
                    number=self._logged_trades + offset + 1,
                )
            except OSError as exc:
                log.warning("could not write the trade log: %s", exc)
                return
            log.info(
                "TRADE CLOSED %s %.8f  %.2f -> %.2f  net %+.2f %s  (%s)",
                trade.side.value, trade.qty, trade.entry_price, trade.exit_price,
                trade.net_pnl, self.config.account.currency, trade.reason,
            )
        self._logged_trades = len(self.portfolio.trades)

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
        if self.portfolio.trades:
            print(tradelog.render(
                self.portfolio.trades,
                starting_cash=self.config.account.starting_cash,
                currency=self.config.account.symbol,
                limit=20,
            ))
            if self.config.live.trades_file:
                print(f"  Full trade log: {self.config.live.trades_file}\n")
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
            "started_at": self._started_at,
            "symbol": self.config.market.symbol,
            "interval": self.config.market.interval,
            "strategy": self.strategy.name,
            "last_bar_ts": self._last_bar_ts,
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

        # Keep the original start time across restarts, so a resumed run still reports
        # its true age rather than restarting the clock.
        self._started_at = int(payload.get("started_at") or self._started_at)
        # Resuming into the bar the previous process already handled would repeat it.
        self._last_bar_ts = max(self._last_bar_ts, int(payload.get("last_bar_ts") or 0))
        self.engine.restore(payload["engine"])
        log.info(
            "resumed from %s: position %.8f, cash %.2f%s",
            path, self.portfolio.qty, self.portfolio.cash,
            f", HALTED ({self.risk.halted_reason})" if self.risk.halted_reason else "",
        )
        self._warn_if_overdrawn()
        return True

    def _warn_if_overdrawn(self) -> None:
        """Flag a resumed position that was funded with money the account never had.

        An earlier version sized a position from equity and then charged the fee on
        top, leaving cash a little below zero - an unfunded overdraft the simulation
        invented. The engine no longer does that, but a state file written back then
        still carries it, and resuming inherits it.

        This warns rather than corrects. Silently adjusting the balance would rewrite
        a recorded trading history to make the software look better, which is a worse
        sin than the original bug; and refusing to resume would strand a bot holding a
        real position. Clearing the state file and starting fresh is the operator's
        call, not this code's.
        """
        if self.portfolio.cash >= 0 or self.portfolio.qty <= 0:
            return
        log.warning(
            "resumed state has cash at %.4f on a long position - this file predates the "
            "solvency fix and carries an overdraft of that size. The figures are off by "
            "about that much; clear %s to start clean.",
            self.portfolio.cash, self.state_path,
        )
