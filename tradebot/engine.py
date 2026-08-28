"""The trading engine: one bar in, orders out.

This is the piece that backtesting and live trading share. Using the same engine for
both is not a tidiness preference - it is the only way to be confident that what you
tested is what will run. If the simulator and the live path had separate logic, every
difference between them would be an unpleasant surprise discovered with real money.

Order of operations within a bar, which is chosen to avoid look-ahead bias:

1. Execute the decision made at the *previous* bar's close, filled at this bar's
   open. You cannot trade on a price until after you have seen it.
2. Check stop-loss and take-profit against this bar's high and low. If both were
   touched in the same bar, assume the stop hit first - the candle does not record
   the order they happened in, so the engine always takes the pessimistic reading.
3. Mark equity at the close.
4. Ask the strategy what it wants, and hold that decision for the next bar.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .brokers.base import Broker, BrokerError
from .brokers.paper import PaperBroker
from .costs import CostModel
from .portfolio import Portfolio
from .risk import RiskManager
from .strategies.base import Context, Strategy
from .types import Candle, Decision, Fill


@dataclass
class ExecutionSettings:
    """Venue mechanics that affect whether an order is even possible.

    ``rebalance_threshold`` is the minimum drift, as a fraction of equity, between the
    position you have and the position you want before the engine will trade to close
    the gap. Without it a strategy asking to stay "100% invested" trades every single
    bar, because entry fees leave cash slightly negative and measured exposure
    therefore sits a hair above its target forever. Those trades are pure cost.

    ``max_resize_cost_share`` caps what a resize may cost as a fraction of the amount
    it moves. This one is load-bearing rather than tidy. With a flat commission the
    fee *itself* creates the drift, so correcting it pays another fee and drifts
    further - a runaway loop that turns buy-and-hold into hundreds of trades and can
    empty a small account on costs alone. Refusing to pay more than a tenth of the
    moved notional to move it breaks the cycle.

    Opening and closing are never blocked by either setting - only resizing.
    """

    qty_step: float = 0.0
    min_notional: float = 10.0
    rebalance_threshold: float = 0.005
    max_resize_cost_share: float = 0.10

    def round_qty(self, qty: float) -> float:
        """Round down to the venue's lot size, so an order is never larger than intended."""
        if self.qty_step <= 0:
            return qty
        steps = math.floor(abs(qty) / self.qty_step)
        return math.copysign(steps * self.qty_step, qty)


@dataclass
class Engine:
    strategy: Strategy
    portfolio: Portfolio
    risk: RiskManager
    costs: CostModel
    broker: Broker | None = None
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)

    pending: Decision | None = None
    active_stop: float | None = None
    active_target: float | None = None
    bars_seen: int = 0

    _log: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.broker is None:
            self.broker = PaperBroker(self.costs)

    @property
    def journal(self) -> list[str]:
        return list(self._log)

    # ------------------------------------------------------------------ per bar

    def process(self, candle: Candle) -> list[Fill]:
        """Run one bar through the full pipeline. Returns any fills it produced."""
        fills: list[Fill] = []
        self.bars_seen += 1

        if self.pending is not None:
            fills += self._execute(self.pending, candle.open, candle.ts)
            self.pending = None

        fills += self._check_brackets(candle)

        self.risk.observe(candle.ts, self.portfolio, candle.close)
        self.portfolio.mark(candle.ts, candle.close)

        ctx = Context(
            exposure=self.portfolio.exposure(candle.close),
            equity=self.portfolio.equity(candle.close),
            costs=self.costs,
            avg_price=self.portfolio.avg_price,
        )
        decision = self.strategy.on_candle(candle, ctx)

        if self.bars_seen <= self.strategy.warmup:
            self.pending = None
            return fills

        self.pending = decision
        return fills

    def _check_brackets(self, candle: Candle) -> list[Fill]:
        """Exit on stop-loss or take-profit if this bar's range touched either."""
        if self.portfolio.is_flat:
            self.active_stop = None
            self.active_target = None
            return []

        long = self.portfolio.is_long
        stop, target = self.active_stop, self.active_target

        stop_hit = stop is not None and (candle.low <= stop if long else candle.high >= stop)
        target_hit = target is not None and (candle.high >= target if long else candle.low <= target)

        if not stop_hit and not target_hit:
            return []

        # Both touched inside one bar: the candle cannot tell us which came first, so
        # assume the loss did. Anything else would flatter the backtest.
        #
        # Gaps are then resolved by order type, which cuts both ways. A stop is a
        # market order: if the bar opened beyond it, the position is sold into the gap
        # at the open, worse than the stop price. A take-profit is a limit order: if
        # the bar opened beyond it, it fills at the open, better than the target.
        # Filling both at the trigger price - the obvious implementation - flatters
        # stops and penalises targets, which quietly distorts any strategy whose
        # brackets sit inside a typical bar's range.
        if stop_hit:
            reason = "stop loss"
            exit_price = min(stop, candle.open) if long else max(stop, candle.open)
        else:
            reason = "take profit"
            exit_price = max(target, candle.open) if long else min(target, candle.open)

        self.pending = None  # the bracket overrides whatever the last bar wanted
        return self.close_position(float(exit_price), candle.ts, reason)

    # ------------------------------------------------------------------ execution

    def _execute(self, decision: Decision, price: float, ts: int) -> list[Fill]:
        verdict = self.risk.evaluate(decision.target_weight, self.portfolio, price)
        if not verdict.approved and abs(verdict.target_weight - self.portfolio.exposure(price)) < 1e-9:
            if verdict.reason.startswith("halted"):
                self._record(ts, f"blocked: {verdict.reason}")
            return []

        target = verdict.target_weight
        equity = self.portfolio.equity(price)
        if equity <= 0:
            return []

        current = self.portfolio.exposure(price)
        resizing = abs(target) > 1e-9 and abs(current) > 1e-9
        if resizing and not self._resize_is_worth_it(target, current, equity):
            self._refresh_brackets(decision)
            return []

        target_qty = (target * equity) / price
        delta = target_qty - self.portfolio.qty
        delta = self.execution.round_qty(delta)

        if abs(delta) < 1e-12 or abs(delta * price) < self.execution.min_notional:
            # Still refresh the brackets even when no trade is needed, so a strategy can
            # trail its stop on a position it is already holding.
            self._refresh_brackets(decision)
            return []

        fill = self._submit(ts, delta, price, decision.reason)
        self._refresh_brackets(decision)
        if fill is None:
            return []
        self._record(ts, f"{fill.side.value} {fill.qty:.8f} @ {fill.price:.2f} - {decision.reason}")
        return [fill]

    def _resize_is_worth_it(self, target: float, current: float, equity: float) -> bool:
        """Should we trade to close the gap between the position held and wanted?

        Only when the gap is big enough to be worth noticing, and only when fixing it
        costs meaningfully less than the amount being moved. Both guards apply to
        resizes alone; opening and closing always go through.
        """
        drift = abs(target - current)
        if drift < self.execution.rebalance_threshold:
            return False

        moved = drift * equity
        if moved <= 0:
            return False

        one_way_cost = self.costs.fee(moved) + moved * (
            (self.costs.half_spread_bps + self.costs.slippage_bps) * 1e-4
        )
        return one_way_cost <= moved * self.execution.max_resize_cost_share

    def close_position(self, price: float, ts: int, reason: str) -> list[Fill]:
        if self.portfolio.is_flat:
            return []
        fill = self._submit(ts, -self.portfolio.qty, price, reason)
        self.active_stop = None
        self.active_target = None
        if fill is None:
            return []
        self._record(ts, f"{reason}: {fill.side.value} {fill.qty:.8f} @ {fill.price:.2f}")
        return [fill]

    def _submit(self, ts: int, signed_qty: float, reference_price: float, reason: str) -> Fill | None:
        """Send an order to the broker and book whatever came back.

        A broker returning ``None`` means nothing was executed - a dry run, a rejected
        order, or a size that rounded away. In that case the books must not move, which
        is why the portfolio is only updated on a real fill.
        """
        assert self.broker is not None  # set in __post_init__
        try:
            fill = self.broker.execute(ts, signed_qty, reference_price, reason)
        except BrokerError as exc:
            self._record(ts, f"order rejected: {exc}")
            return None
        if fill is None:
            return None

        for trade in self.portfolio.apply(fill):
            self.risk.record_trade_result(trade.net_pnl)
        return fill

    def _refresh_brackets(self, decision: Decision) -> None:
        if self.portfolio.is_flat:
            self.active_stop = None
            self.active_target = None
            return
        self.active_stop = decision.stop_loss
        self.active_target = decision.take_profit

    def _record(self, ts: int, message: str) -> None:
        self._log.append(f"{ts} {message}")
        if len(self._log) > 5000:
            del self._log[:1000]

    # ------------------------------------------------------------------ state

    def state(self) -> dict:
        return {
            "portfolio": self.portfolio.state(),
            "risk": self.risk.state(),
            "active_stop": self.active_stop,
            "active_target": self.active_target,
            "bars_seen": self.bars_seen,
        }

    def restore(self, state: dict) -> None:
        self.portfolio.restore(state["portfolio"])
        self.risk.restore(state["risk"])
        self.active_stop = state.get("active_stop")
        self.active_target = state.get("active_target")
        self.bars_seen = int(state.get("bars_seen", 0))
