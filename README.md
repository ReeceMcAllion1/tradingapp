# tradebot

A fully automated trading system: it downloads market data, decides, sizes positions,
places orders, manages risk, and keeps running unattended. It is built to be honest
about what automated trading actually is.

**Start here:**

```bash
python3 -m tradebot demo
```

No dependencies, no install. Python 3.11 or newer.

---

## Read this before anything else

You asked for a system that makes money, and that takes a profit even if it is only a
few pence. I built the system. I have to be straight with you about the second part,
because it is the thing that decides whether you end up better or worse off.

**1. No software can guarantee a profit.** Not this, not anything sold to you. Prices
are close to unpredictable over short horizons, and the people on the other side of
your trades are largely institutions with faster connections, better data and lower
fees than you. Most retail algorithmic traders lose money. That is not pessimism, it
is the base rate.

**2. "Take any profit, even a few pence" is the one strategy I can tell you loses
before running it.** Every round trip costs you the spread twice, slippage twice and
commission twice — roughly 0.28% on a retail crypto account. On a £1,000 position
that is £2.80. A 5p profit target does not lose you 5p of edge, it loses you £2.75
per trade, and it does that on the trades that *win*.

To be precise, because the accurate version is more useful than the scary one: a
*fixed cash* target is not doomed at every size. Cost scales with the position while
5p does not, so 5p clears 0.28% on any position under about £18. That is the entire
loophole and it is not much of one — you tie up £18 to earn 5p, need a 0.28% move to
do it, and most venues will not accept an order that small. Twenty wins a day, with
no losers at all, is £1.

What has no loophole is a target set as a *percentage* below the round-trip cost —
"sell as soon as I'm up a bit". Then both sides scale together, position size cancels
out, and it loses at every size, forever.

**3. So the system is built to show you this rather than argue with you.** The
`micro_scalp` strategy implements exactly what you asked for, faithfully. Run the
demo and look at it: on real BTC data it calls the direction correctly on ~97% of its
trades and still loses about 10% of the account in a week. The signal is fine. The
trades are too small to survive the cost of placing them.

**4. What actually has a chance:** trading less often, and only when the move you are
aiming for is several times bigger than the round trip. That is what `mean_reversion`
and `ema_cross` do. They will still lose money much of the time — in the demo above,
all three strategies lose. That is the honest result, not a bug.

**5. Never run this with money you cannot afford to lose entirely.** Start with paper
trading, and stay there for weeks.

This is not financial advice. In the UK, crypto trading is unregulated and not covered
by the FSCS or the Financial Ombudsman; you have no protection if it goes wrong. If
you are trading CFDs instead, brokers are required to disclose that 70–80% of retail
accounts lose money, and they are not exaggerating.

---

## What you get

```
python3 -m tradebot demo           the cost arithmetic, run on real data
python3 -m tradebot strategies     list available strategies
python3 -m tradebot fetch          download history to CSV
python3 -m tradebot backtest       replay a strategy over history
python3 -m tradebot paper          run automated: live data, simulated money
python3 -m tradebot verify-keys    read-only check that your API keys work
python3 -m tradebot live           run automated with real money (four gates)
python3 -m tradebot init-config    write a starter config.toml
```

| Piece | What it does |
|---|---|
| `costs.py` | Spread, slippage and commission, charged on every simulated fill |
| `risk.py` | Position caps, daily loss limit, drawdown kill switch, cooldowns |
| `engine.py` | The decision → risk → order pipeline, shared by backtest and live |
| `portfolio.py` | Cash, positions and P&L, split into gross / slippage / commission |
| `backtest.py` | Historical replay with no look-ahead |
| `live.py` | The unattended runner, with state that survives restarts |
| `brokers/` | Paper execution, and a hard-gated Crypto.com live adapter |
| `feeds/` | Crypto.com public data, CSV files, and a synthetic generator |
| `strategies/` | Three strategies, including the one that demonstrates the problem |

Run the tests with `python3 -m unittest discover -s tests -t .` — there are 95, and
they cover the accounting, the risk limits, and the ways backtesters usually lie.

---

## Getting started

```bash
git clone <this repo> && cd tradingapp
python3 -m tradebot demo              # understand the costs first
python3 -m tradebot init-config       # writes config.toml
```

Then edit `config.toml`, especially `[costs]` — put in your venue's real fees, then
make them slightly worse. Everything the system reports is only as honest as those
four numbers.

```bash
python3 -m tradebot fetch --bars 5000 -o data/history.csv
python3 -m tradebot backtest --csv data/history.csv -s ema_cross --trades 20
python3 -m tradebot paper             # automated, live data, simulated money
```

Leave the paper run going for weeks. Use `nohup`, `screen`, or a systemd unit — it
saves state after every bar, so it survives restarts.

### How a backtest avoids lying to you

Most backtests are optimistic in four specific ways. This one is not:

- **Look-ahead.** A decision made at a bar's close is filled at the *next* bar's
  open. You cannot trade on a price before you have seen it.
- **Costs.** Spread, slippage and commission are charged on every fill, and reported
  separately from the market move so you can see exactly what the venue took.
- **Ambiguous bars.** If a bar's range touched both your stop and your target, the
  engine assumes the stop hit first. A candle cannot say which came first, so it
  takes the pessimistic reading.
- **Indicators.** All of them are streaming and see one bar at a time, so a backtest
  and a live run compute identical values.

It still cannot model outages, partial fills, thin liquidity, or the fact that you
chose the strategy after seeing the data. A backtest is a best case. Treat one that
barely breaks even as a loser.

---

## Risk controls

These are what make the system safe to leave running. All are in `[risk]`.

| Setting | Default | What it does |
|---|---|---|
| `max_position_pct` | 25% | Never put more than this into one position |
| `max_daily_loss_pct` | 2% | Down this much today → pause until tomorrow |
| `max_drawdown_pct` | 20% | Down this much from peak → **kill switch**, stop permanently |
| `max_trades_per_day` | 20 | Overtrading is how you pay fees for nothing |
| `min_edge_multiple` | 2× | A signal must target twice the round-trip cost to qualify |
| `cooldown_bars_after_loss` | 3 | Sit out a few bars after a loser |
| `allow_short` | off | Shorting is modelled without borrow cost or margin calls |

The daily loss limit lifts overnight. The drawdown kill switch does not — a drawdown
that deep usually means the strategy has stopped working rather than that it is
having an unlucky hour, so clearing it is a decision for you, by deleting the state
file. Closing a position is always permitted, even while halted: a risk limit must
never trap you in a trade.

---

## Going live

Live trading is off by default and takes four independent steps to arm. This is
deliberate — each one is a place to stop and reconsider.

1. `enabled = true` in `[live]`
2. `CRYPTOCOM_API_KEY` and `CRYPTOCOM_API_SECRET` in your **environment**, never in
   the config file (which can end up in git)
3. `dry_run = false` in `[live]` — until then, orders are logged in full but not sent
4. `--yes-really-trade-live` on the command line, on every run

Plus `max_order_notional`, a hard client-side cap on any single order, enforced in
the broker so nothing can route around it. Start at £10.

```bash
export CRYPTOCOM_API_KEY=...
export CRYPTOCOM_API_SECRET=...
python3 -m tradebot verify-keys        # read-only, places no orders
```

On your exchange API key: enable trading only, **disable withdrawals**, and restrict
it to your IP address. If the key leaks, that is the difference between someone
making bad trades and someone emptying the account.

The live adapter is written against Crypto.com's documented v1 REST API, but I could
not test order placement against a funded account. Run `verify-keys` first, then run
with `dry_run = true` and read the logged orders to check they say what you expect,
then start with the smallest size the exchange allows.

---

## Writing your own strategy

A strategy sees one bar and returns the position it wants. It does not place orders
or know about fees — the engine and risk manager own that.

```python
from tradebot.indicators import RSI
from tradebot.strategies.base import Context, Strategy, register
from tradebot.types import Candle, Decision

@register
class RsiDip(Strategy):
    """Buys oversold conditions, exits when momentum recovers."""

    name = "rsi_dip"

    def __init__(self, period: int = 14, entry: float = 30.0, exit: float = 55.0):
        self.rsi = RSI(period)
        self.entry, self.exit = entry, exit

    @property
    def warmup(self) -> int:
        return self.rsi.period + 1

    def on_candle(self, candle: Candle, ctx: Context) -> Decision:
        value = self.rsi.update(candle.close)
        if value is None:
            return Decision(0.0, reason="warming up")
        if ctx.is_flat and value < self.entry:
            return Decision(1.0, stop_loss=candle.close * 0.98, reason=f"oversold ({value:.0f})")
        if not ctx.is_flat and value > self.exit:
            return Decision(0.0, reason=f"recovered ({value:.0f})")
        return Decision(ctx.exposure, reason="holding")
```

Import it in `tradebot/strategies/__init__.py` and it appears in the CLI.

`ctx.breakeven_move_pct` tells you what a round trip costs. Check every signal against
it before returning a non-zero weight — that one habit separates a strategy that might
work from one that cannot. If your strategy has a fixed profit target, implement
`cost_warnings()` so it complains about impossible parameters before it trades.

---

## If you want this to actually make money

The honest path, in order:

1. Run `demo`. Understand the cost arithmetic until it is obvious.
2. Put your real fees in `[costs]`.
3. Backtest over *years*, not days, and across both rising and falling markets.
4. Paper trade for at least a month. Compare it to the backtest — if live paper
   results are much worse, your cost model is too kind.
5. Only then consider real money, at the smallest size the exchange allows.
6. Expect to lose. Size everything so that being wrong is survivable.

The largest edge available to you is not a better strategy. It is trading less,
paying less, and not blowing up — which is exactly what the risk limits in this
system are for.
