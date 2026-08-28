"""Risk limits, sizing and the kill switch.

Every decision a strategy makes passes through here before it can become an order.
The strategy proposes; risk disposes. The limits below are the difference between a
bad week and a blown account, and they are the reason this system is safe to leave
running unattended.

Nothing here tries to make money. It only ever makes positions smaller or refuses
them outright.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .costs import CostModel
from .portfolio import Portfolio


def _day_of(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass
class RiskLimits:
    """Hard bounds on what the system is allowed to do.

    ``max_daily_loss_pct`` pauses trading until the next UTC day.
    ``max_drawdown_pct`` is the kill switch: it stops the system permanently until a
    human clears the state file, because a drawdown that deep usually means the
    strategy has stopped working rather than that it is having an unlucky hour.
    """

    max_position_pct: float = 0.25
    max_daily_loss_pct: float = 0.02
    max_drawdown_pct: float = 0.20
    max_trades_per_day: int = 20
    min_trade_notional: float = 10.0
    min_edge_multiple: float = 2.0
    cooldown_bars_after_loss: int = 3
    allow_short: bool = False

    def __post_init__(self) -> None:
        if not 0 < self.max_position_pct <= 1.0:
            raise ValueError("max_position_pct must be in (0, 1]")
        if not 0 < self.max_daily_loss_pct <= 1.0:
            raise ValueError("max_daily_loss_pct must be in (0, 1]")
        if not 0 < self.max_drawdown_pct <= 1.0:
            raise ValueError("max_drawdown_pct must be in (0, 1]")
        if self.min_trade_notional < 0:
            raise ValueError("min_trade_notional must not be negative")


@dataclass
class RiskVerdict:
    """The risk manager's answer: how much of the requested position is allowed."""

    target_weight: float
    approved: bool
    reason: str = ""


@dataclass
class RiskManager:
    limits: RiskLimits
    costs: CostModel

    peak_equity: float = 0.0
    day: str = ""
    day_start_equity: float = 0.0
    trades_today: int = 0
    cooldown_left: int = 0
    halted_reason: str | None = None

    _events: list[str] = field(default_factory=list)

    @property
    def events(self) -> list[str]:
        return list(self._events)

    def _log(self, message: str) -> None:
        self._events.append(message)

    # ------------------------------------------------------------------ per-bar

    def observe(self, ts: int, portfolio: Portfolio, price: float) -> None:
        """Update daily and drawdown bookkeeping. Call once per bar, before ``evaluate``."""
        equity = portfolio.equity(price)
        today = _day_of(ts)

        if today != self.day:
            self.day = today
            self.day_start_equity = equity
            self.trades_today = 0
            if self.halted_reason == "daily loss limit":
                self.halted_reason = None
                self._log(f"{today}: new day, daily loss halt lifted")

        self.peak_equity = max(self.peak_equity, equity)

        if self.halted_reason is None and self.peak_equity > 0:
            drawdown = 1.0 - equity / self.peak_equity
            if drawdown >= self.limits.max_drawdown_pct:
                self.halted_reason = "max drawdown"
                self._log(
                    f"{today}: KILL SWITCH - drawdown {drawdown:.1%} hit the "
                    f"{self.limits.max_drawdown_pct:.1%} limit; trading stopped"
                )

        if self.halted_reason is None and self.day_start_equity > 0:
            day_loss = 1.0 - equity / self.day_start_equity
            if day_loss >= self.limits.max_daily_loss_pct:
                self.halted_reason = "daily loss limit"
                self._log(f"{today}: down {day_loss:.1%} today; trading paused until tomorrow")

        if self.cooldown_left > 0:
            self.cooldown_left -= 1

    def evaluate(self, requested: float, portfolio: Portfolio, price: float) -> RiskVerdict:
        """Clamp a strategy's requested weight to something the account can survive.

        While halted the answer is always a target of zero, which means an open
        position gets closed rather than frozen. That is deliberate: a halt stops the
        strategy from managing the trade, and an unmanaged position with no one
        watching its stop is the worst of both worlds. Stopping trading has to mean
        stopping exposure too.
        """
        if self.halted_reason is not None:
            return RiskVerdict(0.0, approved=False, reason=f"halted: {self.halted_reason}")

        if not self.limits.allow_short and requested < 0:
            requested = 0.0

        capped = max(-self.limits.max_position_pct, min(self.limits.max_position_pct, requested))
        current = portfolio.exposure(price)
        equity = portfolio.equity(price)

        if equity <= 0:
            return RiskVerdict(0.0, approved=False, reason="no equity left")

        increasing = abs(capped) > abs(current) + 1e-9
        closing = abs(capped) < abs(current) - 1e-9

        # Closing a position is always allowed - risk limits must never trap you in a
        # trade. Everything below only ever blocks opening or adding.
        if closing or abs(capped - current) < 1e-9:
            return RiskVerdict(capped, approved=True, reason="within limits")

        if self.cooldown_left > 0:
            return RiskVerdict(current, approved=False, reason=f"cooldown ({self.cooldown_left} bars left)")

        if self.trades_today >= self.limits.max_trades_per_day:
            return RiskVerdict(current, approved=False, reason="daily trade cap reached")

        if increasing:
            delta_notional = abs(capped - current) * equity
            if delta_notional < self.limits.min_trade_notional:
                return RiskVerdict(current, approved=False, reason="below minimum trade size")

        return RiskVerdict(capped, approved=True, reason="within limits")

    def expected_edge_covers_costs(self, expected_move_pct: float) -> bool:
        """Is a predicted move big enough to be worth paying the spread for?

        A strategy that expects to capture less than ``min_edge_multiple`` times the
        round-trip cost is not trading, it is donating. Strategies can call this to
        filter their own signals; ``MicroScalp`` deliberately does not, which is what
        makes the demo so bleak.
        """
        return abs(expected_move_pct) >= self.limits.min_edge_multiple * self.costs.breakeven_move_pct()

    def record_trade_result(self, net_pnl: float) -> None:
        self.trades_today += 1
        if net_pnl < 0:
            self.cooldown_left = self.limits.cooldown_bars_after_loss

    def state(self) -> dict:
        return {
            "peak_equity": self.peak_equity,
            "day": self.day,
            "day_start_equity": self.day_start_equity,
            "trades_today": self.trades_today,
            "cooldown_left": self.cooldown_left,
            "halted_reason": self.halted_reason,
        }

    def restore(self, state: dict) -> None:
        self.peak_equity = float(state.get("peak_equity", 0.0))
        self.day = str(state.get("day", ""))
        self.day_start_equity = float(state.get("day_start_equity", 0.0))
        self.trades_today = int(state.get("trades_today", 0))
        self.cooldown_left = int(state.get("cooldown_left", 0))
        self.halted_reason = state.get("halted_reason")
