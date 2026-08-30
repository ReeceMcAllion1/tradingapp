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
class AlpacaConfig:
    """Route data and orders through Alpaca instead of Crypto.com.

    Off by default. When ``enabled`` is true, ``fetch``, ``backtest``, ``paper`` and
    ``live`` use Alpaca's market data and (for ``paper``/``live``) Alpaca's brokerage.
    Credentials are read from the environment only - ``APCA_API_KEY_ID`` and
    ``APCA_API_SECRET_KEY`` - never from this file.

    ``paper = true`` points the broker at ``paper-api.alpaca.markets``: real orders,
    real position bookkeeping, no real money. It is still not "live" for the purposes
    of the real-money command gates. Setting ``paper = false`` is what arms real
    trading, and it obeys the same ``[live]`` gates and ``--yes-really-trade-live``
    flag as the Crypto.com path.

    ``asset_class`` picks the market: ``"crypto"`` (symbols like ``BTC/USD``, trades
    24/7) or ``"us_equity"`` (symbols like ``AAPL``, regular US market hours only).
    ``data_feed`` applies to equities only: ``"iex"`` is free, ``"sip"`` needs a paid
    Alpaca data subscription.
    """

    enabled: bool = False
    asset_class: str = "crypto"
    paper: bool = True
    data_feed: str = "iex"

    def __post_init__(self) -> None:
        if self.asset_class not in ("crypto", "us_equity"):
            raise ValueError(
                f"alpaca.asset_class must be 'crypto' or 'us_equity', got {self.asset_class!r}"
            )
        if self.data_feed not in ("iex", "sip"):
            raise ValueError(f"alpaca.data_feed must be 'iex' or 'sip', got {self.data_feed!r}")


@dataclass
class Config:
    market: MarketConfig = field(default_factory=MarketConfig)
    account: AccountConfig = field(default_factory=AccountConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    costs: CostModel = field(default_factory=CostModel)
    risk: RiskLimits = field(default_factory=RiskLimits)
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)
    live: LiveConfig = field(default_factory=LiveConfig)
    alpaca: AlpacaConfig = field(default_factory=AlpacaConfig)

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


def _explain_toml_error(path: Path, exc: Exception) -> str:
    """Turn a TOML parser complaint into something a person can act on.

    One case is worth catching by hand, because the error it produces is actively
    misleading. A Windows path pasted into a double-quoted TOML value -
    ``state_file = "C:\\Users\\me\\bot\\state.json"`` - is not a path as far as TOML
    is concerned: the backslash starts an escape sequence, ``\\U`` begins a Unicode
    escape, and the parser reports "Invalid hex value" at a column the reader has no
    reason to connect to their own file path. Nobody guesses that from the message.

    Forward slashes work perfectly well on Windows, so that is the fix offered first.
    """
    message = f"{path} is not valid TOML: {exc}"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return message

    suspects = [
        line.strip() for line in text.splitlines()
        if '"' in line and "\\" in line and "\\\\" not in line
    ]
    if suspects:
        message += (
            "\n\n  This looks like a Windows path in a quoted value. A backslash starts"
            "\n  an escape sequence in TOML, so a path is read as one and rejected."
            f"\n  The line responsible is probably:\n    {suspects[0]}"
            "\n\n  Use forward slashes, which work fine on Windows:"
            '\n    state_file = "C:/Users/you/bot/state.json"'
            "\n  or double every backslash, or use single quotes, which take the text"
            "\n  literally:"
            "\n    state_file = 'C:\\Users\\you\\bot\\state.json'"
        )
    return message


def load(path: str | Path | None = None) -> Config:
    """Load config from TOML, falling back to defaults when the file is absent."""
    target = Path(path) if path else DEFAULT_PATH
    if not target.exists():
        if path is not None:
            raise FileNotFoundError(f"no config file at {target}")
        config = Config()
        _align_lot_size(config, {})
        return config

    try:
        with target.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(_explain_toml_error(target, exc)) from exc

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
        alpaca=AlpacaConfig(**_subset(AlpacaConfig, raw.get("alpaca", {}), "alpaca")),
    )
    _align_lot_size(config, raw)
    return config


def _align_lot_size(config: Config, raw: dict) -> None:
    """Make the simulator round quantities the way the venue will.

    Two settings describe the same physical fact from opposite ends: ``execution.
    qty_step`` is the lot size the backtester rounds to, and ``live.qty_decimals`` is
    the precision the venue accepts. Nothing kept them in step, and their defaults
    disagree - the simulator rounded to nothing at all while the live broker floors to
    six decimals. Set ``qty_decimals = 2`` for your venue and the backtest happily
    filled sizes the exchange would truncate, which is exactly the paper-versus-live
    divergence this package exists to avoid.

    So when ``qty_step`` has been left alone, derive it from the venue precision. An
    explicit ``qty_step`` in the file always wins: someone who wrote a lot size meant
    it, and a venue with a step that is not a power of ten (0.25, say) cannot be
    expressed as a number of decimals at all.
    """
    if "qty_step" in raw.get("execution", {}):
        return
    decimals = config.live.qty_decimals
    if decimals < 0:
        raise ValueError("live.qty_decimals must not be negative")
    config.execution.qty_step = 10.0**-decimals
