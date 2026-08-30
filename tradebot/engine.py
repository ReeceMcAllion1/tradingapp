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
from .types import Candle, Decision, Fill, Liquidity, Side


@dataclass
class ExecutionSettings:
    """Venue mechanics that affect whether an order is even possible.

    Both settings below exist because any target weight held below 100% drifts as the
    price moves - hold half your equity in something and a rally makes it 52% - so an
    engine that corrects every drift trades on every single bar, for nothing. They cover
    different fee regimes and neither is redundant; ``tests/test_adversarial.py`` pins
    each in the regime where it is the only thing standing.

    ``rebalance_threshold`` is the minimum drift, as a fraction of equity, worth trading
    to close. It is the guard that matters when commission is purely proportional: a
    rebalance then costs a fixed *fraction* of what it moves however small it is, so the
    cost cap below never objects, and without the threshold a fixed-weight hold trades
    on essentially every bar.

    ``max_resize_cost_share`` caps what a resize may cost as a fraction of the amount it
    moves. This is the guard for flat commissions and small accounts, where the fee
    *itself* creates the drift: correcting it pays another fee and drifts again. Half a
    percent of a £200 account is a £1 rebalance paying a £2 fee - a move the threshold
    waves through - and unguarded that loop spends more than the whole account on
    commission. Refusing to pay more than a tenth of the moved notional to move it
    breaks the cycle.

    Neither is what keeps a *fully* invested position still. That was their original
    job, and they did it by suppressing a symptom: sizing from equity and then charging
    the fee left cash slightly negative, so measured exposure sat permanently above its
    target and the engine chased a gap that was pure accounting. ``_affordable`` removed
    the cause, and a 100%-invested hold now sits at exactly 1.0 with these two turned
    off entirely.

    ``max_consecutive_rejections`` halts trading after the venue refuses this many
    orders in a row. Without it a persistently rejected order - wrong symbol, too
    small, insufficient funds, expired key - is retried on every single bar forever,
    hammering the API unattended and never getting anywhere. A rejection that repeats
    is a condition a human needs to look at, not one to retry into.

    Opening and closing are never blocked by either setting - only resizing.

    ``maker_offset_bps`` turns market orders into resting limit orders, priced this far
    on the passive side of the reference. Venues charge makers less than takers - half,
    on the defaults here - and halving the fee is the one improvement available that
    needs no forecast at all. It is off by default (0.0) because the discount is not
    free, and the reason it is not free is the whole point of modelling it:

    A resting buy only fills if price comes down to it. If price runs away upward you
    do not fill, and you miss the move you wanted. So you are systematically filled on
    the trades that immediately go against you and left behind on the ones that would
    have worked. That is adverse selection, and it is paid in returns rather than fees,
    which is exactly why it is invisible to anyone who models the discount but not the
    miss. Whether the cheaper fee outweighs it is an empirical question this setting
    exists to answer honestly rather than assume.

    ``maker_queue_bps`` is the pessimism dial on all of the above, and it exists because
    the obvious model of a limit fill is too kind. Assuming that price merely *touching*
    your limit fills you ignores the queue: at a popular price there are other orders
    ahead of yours, and a level that is tagged once and bounces fills them, not you.
    Requiring price to trade this far *through* the limit before counting a fill models
    being at the back of that queue. Nought means the optimistic reading; raise it to
    find out how much of the gain depends on getting filled easily.

    ``maker_max_wait_bars`` is how long an unfilled order rests before it is given up
    on. ``maker_then_take`` decides what happens then: cross the spread and pay the
    taker fee (the position is reached, late and at a worse price), or abandon the
    order entirely (no trade, no cost, no position). Stops and risk halts never rest -
    getting out is always a market order, because a limit exit in a crash is an order
    that does not fill precisely when you need it to.
    """

    qty_step: float = 0.0
    min_notional: float = 10.0
    rebalance_threshold: float = 0.005
    max_resize_cost_share: float = 0.10
    max_consecutive_rejections: int = 5
    maker_offset_bps: float = 0.0
    maker_max_wait_bars: int = 1
    maker_then_take: bool = True
    maker_queue_bps: float = 0.0

    def round_qty(self, qty: float) -> float:
        """Round down to the venue's lot size, so an order is never larger than intended."""
        if self.qty_step <= 0:
            return qty
        steps = math.floor(abs(qty) / self.qty_step)
        return math.copysign(steps * self.qty_step, qty)


@dataclass
class RestingOrder:
    """A limit order sitting on the book, waiting for price to come to it."""

    decision: Decision
    signed_qty: float
    limit_price: float
    bars_waited: int = 0


@dataclass
class Engine:
    strategy: Strategy
    portfolio: Portfolio
    risk: RiskManager
    costs: CostModel
    broker: Broker | None = None
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)

    pending: Decision | None = None
    resting: RestingOrder | None = None
    active_stop: float | None = None
    active_target: float | None = None
    bars_seen: int = 0
    consecutive_rejections: int = 0
    maker_fills: int = 0
    maker_misses: int = 0

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

        # A limit order placed on an earlier bar is still on the book: give it this
        # bar's range before deciding anything new.
        if self.resting is not None:
            fills += self._work_resting(candle)

        if self.pending is not None:
            fills += self._execute(self.pending, candle.open, candle.ts, candle)
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
        self.resting = None  # and cancels any limit order still on the book
        return self.close_position(float(exit_price), candle.ts, reason)

    # ------------------------------------------------------------------ execution

    def _execute(self, decision: Decision, price: float, ts: int,
                 candle: Candle | None = None) -> list[Fill]:
        if decision.is_hold:
            # "Change nothing." The only thing that overrides it is a risk halt, which
            # must always be able to flatten - see RiskManager.evaluate.
            if self.risk.halted_reason is not None and not self.portfolio.is_flat:
                self._record(ts, f"blocked: halted: {self.risk.halted_reason}")
                return self.close_position(price, ts, f"halted: {self.risk.halted_reason}")
            self._refresh_brackets(decision)
            return []

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
        if delta > 0:
            delta = min(delta, self._affordable(price))
        delta = self.execution.round_qty(delta)

        if abs(delta) < 1e-12 or abs(delta * price) < self.execution.min_notional:
            # Still refresh the brackets even when no trade is needed, so a strategy can
            # trail its stop on a position it is already holding.
            self._refresh_brackets(decision)
            return []

        if self.execution.maker_offset_bps > 0 and candle is not None:
            self._refresh_brackets(decision)
            return self._rest_order(decision, delta, price, candle)

        fill = self._submit(ts, delta, price, decision.reason)
        self._refresh_brackets(decision)
        if fill is None:
            return []
        self._record(ts, f"{fill.side.value} {fill.qty:.8f} @ {fill.price:.2f} - {decision.reason}")
        return [fill]

    # ------------------------------------------------------------------ maker orders

    def _rest_order(self, decision: Decision, signed_qty: float,
                    reference_price: float, candle: Candle) -> list[Fill]:
        """Place a limit order on the passive side and see if this bar reaches it."""
        offset = self.execution.maker_offset_bps * 1e-4
        if signed_qty > 0:
            limit = reference_price * (1.0 - offset)
        else:
            limit = reference_price * (1.0 + offset)

        self.resting = RestingOrder(decision=decision, signed_qty=signed_qty, limit_price=limit)
        self._record(candle.ts,
                     f"resting {'buy' if signed_qty > 0 else 'sell'} {abs(signed_qty):.8f} "
                     f"@ {limit:.2f} - {decision.reason}")
        return self._work_resting(candle)

    def _work_resting(self, candle: Candle) -> list[Fill]:
        """Fill, keep waiting, or give up on the order currently on the book.

        A buy fills only if the bar traded down to the limit; a sell only if it traded
        up to it. A bar that gaps straight past the limit fills at the open instead,
        which is *better* than the limit - the book cannot fill you worse than the price
        you named, and that asymmetry is the one thing a limit order buys you.

        Not filling is the cost. Price ran away, the position was never taken, and
        whatever that move was worth is gone. The counters make that visible rather
        than letting a good fill rate be assumed.

        Brackets are (re)applied here, on the fill, not when the order was placed. A
        maker entry has no position to protect until it fills - and the ``_refresh_brackets``
        call in ``_execute`` runs while the account is still flat, so it clears the stop
        and returns. Without re-applying it here, a stop named on the entry decision was
        silently dropped and the position rode the next crash uncapped, exactly the bug
        ``_refresh_brackets`` was written to prevent on the taker path.
        """
        order = self.resting
        if order is None:
            return []

        buying = order.signed_qty > 0
        # Price must reach the limit, and - if a queue is being modelled - push through
        # it far enough that the orders ahead of ours would have been cleared first.
        through = order.limit_price * (1.0 - self.execution.maker_queue_bps * 1e-4) if buying \
            else order.limit_price * (1.0 + self.execution.maker_queue_bps * 1e-4)
        touched = candle.low <= through if buying else candle.high >= through

        if touched:
            price = min(order.limit_price, candle.open) if buying else max(order.limit_price, candle.open)
            self.resting = None
            self.maker_fills += 1
            fill = self._submit(candle.ts, order.signed_qty, float(price),
                                order.decision.reason, Liquidity.MAKER)
            if fill is None:
                return []
            self._refresh_brackets(order.decision)
            self._record(candle.ts,
                         f"maker {fill.side.value} {fill.qty:.8f} @ {fill.price:.2f} "
                         f"- {order.decision.reason}")
            return [fill]

        order.bars_waited += 1
        if order.bars_waited < self.execution.maker_max_wait_bars:
            return []

        # Out of patience. Either cross the spread and pay for certainty, or walk away.
        self.resting = None
        self.maker_misses += 1
        if not self.execution.maker_then_take:
            self._record(candle.ts, f"limit never filled, order abandoned - {order.decision.reason}")
            return []

        fill = self._submit(candle.ts, order.signed_qty, candle.close,
                            f"{order.decision.reason} (crossed after {order.bars_waited} bars)")
        if fill is None:
            return []
        self._refresh_brackets(order.decision)
        self._record(candle.ts,
                     f"crossed {fill.side.value} {fill.qty:.8f} @ {fill.price:.2f} "
                     f"- limit missed, paid taker")
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

    def _affordable(self, reference_price: float) -> float:
        """Largest quantity the cash on hand can actually pay for.

        Sizing a position at 100% of *equity* and then charging the fee spends money
        that is not there: cash goes slightly negative, and the account is quietly
        running a small unfunded overdraft. It is only a fraction of a percent, but it
        is money the simulation invented, and it compounds into two visible wrongs -
        measured exposure that sits permanently above its target, and equity that can
        go negative on a long position, which is impossible without leverage.

        Budgeting from cash, net of the fill's own costs, removes the cause rather
        than the symptoms.
        """
        cash = self.portfolio.cash
        if cash <= 0 or reference_price <= 0:
            return 0.0
        fill_price = self.costs.fill_price(Side.BUY, reference_price)
        per_unit = fill_price * (1.0 + self.costs.taker_fee_bps * 1e-4)
        if per_unit <= 0:
            return 0.0
        return max(0.0, (cash - self.costs.flat_fee) / per_unit)

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

    def _submit(self, ts: int, signed_qty: float, reference_price: float, reason: str,
                liquidity: Liquidity = Liquidity.TAKER) -> Fill | None:
        """Send an order to the broker and book whatever came back.

        A broker returning ``None`` means nothing was executed - a dry run, a rejected
        order, or a size that rounded away. In that case the books must not move, which
        is why the portfolio is only updated on a real fill.
        """
        assert self.broker is not None  # set in __post_init__
        try:
            fill = self.broker.execute(ts, signed_qty, reference_price, reason, liquidity)
        except BrokerError as exc:
            self.consecutive_rejections += 1
            self._record(ts, f"order rejected ({self.consecutive_rejections}): {exc}")
            limit = self.execution.max_consecutive_rejections
            if limit > 0 and self.consecutive_rejections >= limit:
                self.risk.halted_reason = "repeated order rejections"
                self._record(ts, f"halting after {self.consecutive_rejections} rejections in a row")
            return None
        if fill is None:
            # Not a rejection: a dry run, or a size that rounded away. Nothing was
            # refused, so the rejection count is left alone.
            return None

        self.consecutive_rejections = 0
        self.risk.record_order()
        for trade in self.portfolio.apply(fill):
            self.risk.record_trade_result(trade.net_pnl)
        return fill

    def _refresh_brackets(self, decision: Decision) -> None:
        """Update the live stop and target from a decision.

        A hold that names no brackets keeps the ones already standing. This looks like
        a detail and is not: "hold, change nothing" previously cleared the stop, so a
        strategy that set a stop on entry and then held with a bare ``Decision(None)``
        lost its protection on the very next bar and rode the next crash uncapped. The
        stop was visible in the entry decision, gone before it could ever trigger, and
        nothing anywhere said so.

        A decision that does name a bracket still replaces the old one, which is what
        lets a trailing stop ratchet. Only silence is treated as silence.
        """
        if self.portfolio.is_flat:
            self.active_stop = None
            self.active_target = None
            return
        if decision.is_hold and decision.stop_loss is None and decision.take_profit is None:
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
            "consecutive_rejections": self.consecutive_rejections,
            "maker_fills": self.maker_fills,
            "maker_misses": self.maker_misses,
        }

    def restore(self, state: dict) -> None:
        self.portfolio.restore(state["portfolio"])
        self.risk.restore(state["risk"])
        self.active_stop = state.get("active_stop")
        self.active_target = state.get("active_target")
        self.bars_seen = int(state.get("bars_seen", 0))
        self.consecutive_rejections = int(state.get("consecutive_rejections", 0))
        self.maker_fills = int(state.get("maker_fills", 0))
        self.maker_misses = int(state.get("maker_misses", 0))
