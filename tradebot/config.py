"""Configuration loading and validation.

Settings come from a TOML file. Anything missing falls back to a conservative
default, and anything nonsensical raises at startup rather than halfway through a
run - a config error discovered at bar 4,000 of a live session is an expensive way to
learn about a typo.

API credentials are deliberately *not* part of this file. They are read from the
environment only, so a config file can be committed to git without leaking anything.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .costs import CostModel
from .engine import ExecutionSettings
from .risk import RiskLimits

DEFAULT_PATH = Path("config.toml")


@dataclass
class MarketConfig:
    symbol: str = "BTC_USD"
    interval: str = "5m"
    history_bars: int = 2000


_CURRENCY_SYMBOLS = {"GBP": "\u00a3", "USD": "$", "EUR": "\u20ac", "JPY": "\u00a5"}


@dataclass
class AccountConfig:
    starting_cash: float = 1000.0
    currency: str = "GBP"

    @property
    def symbol(self) -> str:
        """Display symbol for the currency, falling back to the code itself."""
        return _CURRENCY_SYMBOLS.get(self.currency.upper(), self.currency + " ")


@dataclass
class StrategyConfig:
    name: str = "ema_cross"
    params: dict = field(default_factory=dict)


@dataclass
class LiveConfig:
    """Live trading gates. All of these default to the safe setting."""

    enabled: bool = False
    dry_run: bool = True
    max_order_notional: float = 50.0
    qty_decimals: int = 6
    poll_seconds: float = 15.0
    state_file: str = "state/live_state.json"
    log_file: str = "state/tradebot.log"
    trades_file: str = "state/trades.csv"
    reconcile_every_bars: int = 20


@dataclass
class Config:
    market: MarketConfig = field(default_factory=MarketConfig)
    account: AccountConfig = field(default_factory=AccountConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    costs: CostModel = field(default_factory=CostModel)
    risk: RiskLimits = field(default_factory=RiskLimits)
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)
    live: LiveConfig = field(default_factory=LiveConfig)

    def validate(self) -> list[str]:
        """Return warnings worth showing the user. Errors raise instead."""
        warnings: list[str] = []
        breakeven = self.costs.breakeven_move_pct()
        if breakeven > 1.0:
            warnings.append(
                f"round-trip cost is {breakeven:.2f}% - that is very high; check your fee settings"
            )
        if self.risk.max_position_pct > 0.5:
            warnings.append(
                f"max_position_pct is {self.risk.max_position_pct:.0%}; a single bad trade will hurt"
            )
        if self.risk.allow_short:
            warnings.append("shorting is enabled and is modelled without borrow costs or margin calls")
        if self.live.enabled and not self.live.dry_run:
            warnings.append("LIVE TRADING IS ARMED - orders will be sent with real money")
        return warnings


def _subset(cls, data: dict, section: str) -> dict:
    """Keep only the keys ``cls`` accepts, and complain loudly about the rest.

    A silently ignored setting is worse than an error: you would believe a risk limit
    was in force when it was being dropped on the floor.
    """
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(
            f"[{section}] has unrecognised setting(s): {', '.join(sorted(unknown))}. "
            f"Valid keys are: {', '.join(sorted(known))}"
        )
    return {k: v for k, v in data.items() if k in known}


def load(path: str | Path | None = None) -> Config:
    """Load config from TOML, falling back to defaults when the file is absent."""
    target = Path(path) if path else DEFAULT_PATH
    if not target.exists():
        if path is not None:
            raise FileNotFoundError(f"no config file at {target}")
        return Config()

    with target.open("rb") as handle:
        raw = tomllib.load(handle)

    strategy_raw = dict(raw.get("strategy", {}))
    strategy_params = dict(strategy_raw.pop("params", {}))

    config = Config(
        market=MarketConfig(**_subset(MarketConfig, raw.get("market", {}), "market")),
        account=AccountConfig(**_subset(AccountConfig, raw.get("account", {}), "account")),
        strategy=StrategyConfig(
            **_subset(StrategyConfig, {**strategy_raw, "params": strategy_params}, "strategy")
        ),
        costs=CostModel(**_subset(CostModel, raw.get("costs", {}), "costs")),
        risk=RiskLimits(**_subset(RiskLimits, raw.get("risk", {}), "risk")),
        execution=ExecutionSettings(**_subset(ExecutionSettings, raw.get("execution", {}), "execution")),
        live=LiveConfig(**_subset(LiveConfig, raw.get("live", {}), "live")),
    )
    return config
