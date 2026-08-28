# tradebot

A fully automated trading system: it downloads market data, decides, sizes positions,
places orders, manages risk, and keeps running unattended. It is built to be honest
about what automated trading actually is.

**Start here:**

```bash
python3 -m tradebot demo          # why a few-pence profit target loses money
```

**To run it for days or weeks** (paper money, no API keys, no orders placed):

```bash
./scripts/paper-run.sh start      # three strategies incl. a buy-and-hold benchmark
./scripts/paper-run.sh status     # check on it any time
./scripts/paper-run.sh report     # the verdict, benchmarked against holding
./scripts/paper-run.sh stop
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
trading, and stay there for weeks. Then read `python3 -m tradebot trades` — every
number in this file can be checked against the individual trades that produced it.

This is not financial advice. In the UK, crypto trading is unregulated and not covered
by the FSCS or the Financial Ombudsman; you have no protection if it goes wrong. If
you are trading CFDs instead, brokers are required to disclose that 70–80% of retail
accounts lose money, and they are not exaggerating.

---

## The ten-year test

`python3 -m tradebot study` runs every strategy against ten years of daily stock
data and compares each one to simply buying the stock and holding it. Here is the
result over **2016-08-29 to 2026-08-27**, ten US stocks (SPY, AAPL, MSFT, JNJ, XOM,
KO, GE, F, INTC, BA), 2,513 trading days each, $10,000 per run, dividend- and
split-adjusted prices, costs of 2bp commission + 1bp half-spread + 2bp slippage:

| strategy | final | total | CAGR | max drawdown | trades | costs | vs buy-and-hold |
|---|---|---|---|---|---|---|---|
| **buy_and_hold** | **$44,717** | **+347%** | **13.70%** | 52.9% | 10 | $27 | — |
| ema_cross | $20,838 | +108% | 5.84% | 45.4% | 912 | $1,445 | **−239pp** |
| mean_reversion | $12,031 | +20% | 1.39% | 43.3% | 408 | $414 | **−327pp** |
| micro_scalp | $1,041 | **−90%** | −25.24% | 89.8% | 11,075 | $4,389 | **−437pp** |

**2 of 30 strategy runs beat buying and holding.** Both were single lucky pairings;
with 30 combinations tested, a couple of winners is what chance alone produces.

Three things worth taking from that table.

**Trading lost to not trading, badly.** ema_cross doubled the money and still
finished $24,000 behind doing nothing. It is not a bad strategy — it cut the worst
drawdown from 53% to 45% — it just spent a decade paying costs and sitting in cash
during rallies.

**The few-pence strategy did not merely underperform, it destroyed the account.** It
turned $10,000 into $1,041 while paying **$4,389 in costs** — 44% of the starting
account, spread over 11,075 trades. And it fails twice over: not only do costs exceed
each tiny win, but capping gains at +0.05% while letting losses run to a −2% stop
gives a payoff ratio around 1:2.6, needing a ~72% win rate just to break even before
costs. It got 34%.

**Flat commissions are what actually kills small accounts.** Re-running with a £6
flat fee per trade, typical of a UK broker, on a £1,000 account: buy-and-hold
finished at £8,969 having paid £17 in total costs, while micro_scalp finished at £4
having paid **£1,030 in costs — more than the entire starting account**. At £1,000 a
£6 round trip is 1.3% before the price moves at all.

Reproduce any of it:

```bash
python3 -m tradebot study --years 10
python3 -m tradebot study --years 10 --cash 1000 --flat-fee 6 --symbols SPY,AAPL,MSFT
```

Caveats I would want if I were reading this: it is one decade, one that happened to
be a historic bull market, using tickers that are famous today precisely because they
survived — a survivorship bias that flatters buy-and-hold. It is still the fairest
comparison available, and the direction of the result is not subtle.

## "Only sell if it makes a profit" — tested

The other rule worth testing: **never sell at a loss.** Hold a losing position for as
long as it takes, and close it the moment it is worth more than it cost. No stop loss.
That is the `never_lose` strategy, built exactly as specified — `min_profit_pct = 0`
means it sells the instant the trade nets a single penny after all costs.

It is much better than the tiny-profit-target version, and it is still not the answer.

**On ten stocks that all did fine over the decade:**

| strategy | final | total | vs buy-and-hold |
|---|---|---|---|
| buy_and_hold | $44,717 | +347% | — |
| **never_lose** | **$31,716** | **+217%** | **−130pp** |
| ema_cross (has a stop loss) | $20,838 | +108% | −239pp |

So it beats the stop-loss strategy — in a rising market, refusing to cut is the right
call — and it still finishes $13,000 behind doing nothing.

**Then the test that decides it.** The same rule on companies that fell and did *not*
come back — First Republic Bank (to $0.00), Peloton (−97%), Lumen (−80%), Boeing,
Intel:

| strategy | final | total | vs buy-and-hold |
|---|---|---|---|
| ema_cross (has a stop loss) | $12,624 | +26% | **+16pp** |
| buy_and_hold | $10,994 | +10% | — |
| **never_lose** | **$7,860** | **−21%** | **−31pp** |

The ordering inverts. The strategy with a stop loss goes from worst to best, because
this is the situation stop losses exist for.

**On First Republic specifically**, the rule booked **148 consecutive winning trades**
— a flawless record — and then bought at $209.93 on 10 Dec 2021 and held it for 510
days to $0.35. A $10,000 account ended at **$34**. The same stock, traded by
`ema_cross` with a stop loss, ended at **$18,199**.

That trade never closed voluntarily. It couldn't: the rule only sells at a profit, and
there was never going to be one. It was force-closed by the drawdown kill switch. Left
purely to itself it would have ridden to $0.0004.

### Three things the test showed that are worth keeping

**A 100% win rate is trivially achievable and means nothing.** Ask for any profit
above 0.1% and every single closed trade is a winner, on every stock, always — because
a losing trade is never closed and so never counted. The losses are all still there,
sitting in open positions. "I haven't sold, so I haven't lost" is an accounting story,
not a fact about your money. This is the disposition effect, the most documented
mistake in retail investing, and the win-rate column is exactly how it disguises
itself.

**It inverts the asymmetry you want.** A stop loss caps the downside and lets the
upside run. This rule caps the upside — at whatever tiny profit you set — and leaves
the downside open until the position recovers, or doesn't. On Boeing it sat in one
position for **2,736 days**, seven and a half years, with the money doing nothing else.

**Its own tuning says stop taking small profits.** Sweeping how much profit it demands
before selling, on SPY over ten years:

| demands | trades | win rate | final |
|---|---|---|---|
| 0.1% | 347 | 100% | $24,555 |
| 0.5% | 209 | 100% | $32,915 |
| 2% | 67 | 100% | $38,307 |
| 5% | 30 | 100% | $41,198 |
| never sells | 1 | — | **$41,450** |

Monotonic. The less eagerly it takes a profit, the more it makes — and the limit of
that sweep, demanding infinite profit before selling, *is* buy-and-hold. The strategy's
own parameters point at holding.

```bash
python3 -m tradebot study --years 10 --strategies never_lose
python3 -m tradebot study --years 10 --symbols FRCB,PTON,LUMN,BA,INTC --strategies never_lose,ema_cross
```

## Intraday crypto: 24 more runs, same answer

The stock study used daily bars. Intraday is where people actually expect a bot to
earn its keep, so I ran the same engine over real Crypto.com data at Crypto.com's own
base taker fee (7.5bp), on BTC, ETH and SOL:

**Hourly bars, 20,000 each — 833 days, 2.3 years per instrument:**

| strategy | final | total | trades | costs | **cost/yr** | vs buy-and-hold |
|---|---|---|---|---|---|---|
| buy_and_hold | $882 | −11.8% | 3 | $2 | **0.1%** | — |
| never_lose | $685 | −31.5% | 323 | $266 | 11.6% | −19.7pp |
| ema_cross | $320 | −68.0% | 1,529 | $656 | 28.7% | −56.3pp |
| mean_reversion | $267 | −73.3% | 1,323 | $551 | 24.1% | −61.6pp |
| micro_scalp | $10 | −99.0% | 5,218 | $815 | 35.7% | −87.2pp |

**15-minute bars, 208 days per instrument** — same ordering, worse drag: `ema_cross`
burned **104% of capital per year in costs**, `micro_scalp` 148%.

**0 of 24 runs beat buying and holding.** Across everything now tested — 10 US stocks
over a decade plus 6 crypto series intraday — **2 of 54 runs beat holding**, and both
were isolated pairings of the kind chance produces on its own.

Note the benchmark was *losing* over this crypto window (−11.8%). The active
strategies did not lose because the market fell; they lost three to six times more
than the market did, and the `cost/yr` column is why.

### The one number that decides it

`cost/yr` — costs as a percentage of your capital, annualised — is now reported by
every backtest and blocks in `preflight` above 20%. It is more useful than returns
because returns are noisy and market-dependent while this is not: it is what the venue
takes whether you are right or wrong.

Holding pays 0.1% a year. A 15-minute strategy paid 104%. **No entry signal is good
enough to outrun that**, which is why the fix is always fewer trades or bigger targets,
never a cleverer indicator.

```bash
python3 -m tradebot fetch --bars 20000        # then study any local bar files:
python3 -m tradebot study --files "data/*_1h.csv" --fee-bps 7.5 --cash 1000
```

## The one candidate — and what a sweep did to it

Everything above loses to holding, and the mechanism is always cost drag. So the
question worth asking is whether a trend filter's drawdown protection survives if you
trade rarely enough for costs to stop mattering. That is `slow_trend`: hold above a
long moving average, sit in cash below it, with a band around the line so price
hovering there does not generate trades. Its cost drag is 0.2%/year on daily bars
against `ema_cross`'s 1.4% — 161 trades a decade instead of 912.

At period 200 with a 2% band, on hourly crypto over 2.3 years, it beat buy-and-hold by
13.9 points. **That number should not be trusted, and here is why.**

```
$ python3 -m tradebot sweep -s slow_trend --files "data/*_1h.csv" \
      --param period=100,200,400,600 band_pct=0.0,0.02,0.05

    period  band_pct |       BTC       ETH       SOL      mean   DD cut
       100     0.000 |     -78.9     -37.7     -38.2     -51.6     -8.0
       200     0.020 |     -27.5     +14.4     +54.8     +13.9    +16.2   <- the one I quoted
       400     0.050 |      +3.1     +39.8     +47.0     +30.0    +26.5
       600     0.020 |     +17.3    +140.2     +58.2     +71.9    +26.0

  settings tested               12
  beat buy-and-hold              9  of 12
  best / worst               +71.9 / -51.6 pp
  median                     +18.1 pp
  spread                     123.5 pp
```

The spread across reasonable parameters is **123 points**. Quoting any single cell —
including the +71.9 — reports a choice, not a finding. This is the most common way
backtests mislead, and it catches sincere people, which is why `sweep` is a first-class
command rather than a footnote.

**What actually survives the sweep:**

1. **A mechanism, not a cell.** Results improve monotonically with a longer period and
   a wider band. Both mean fewer trades. That trend is the same cost-drag story as
   everywhere else in this file, and a trend across a grid is evidence in a way that a
   winning cell is not.
2. **Drawdown reduction.** Cut in 9 of 12 settings, median 15.7 points, and it held in
   *every* separate test: 53%→42% on ten US stocks, 88%→64% on the collapsed ones, and
   on all three crypto instruments including the one where it lost money. This is the
   only effect in this entire investigation that has been consistent across markets,
   timeframes, parameters and direction.
3. **It is insurance, not an edge.** Per instrument it lost 27.5 points on BTC, which
   rose, and won 14.4 and 54.8 on ETH and SOL, which fell. Premium paid in up markets,
   payout in down ones. Over the ten-year stock bull run it finished 159 points behind
   holding.

So: no, I did not find a way to make money. I found one effect that is robust
(smaller drawdowns), one that is not (higher returns), and a tool that tells the two
apart.

## The verdict: walk-forward validation

Everything above — every table, including the ones I liked — is in-sample. The
strategies were written after seeing the data and the parameters chosen by running a
grid over the same bars they are reported on. That describes the past with the answer
in hand. It forecasts nothing.

Walk-forward is the correction: split history into consecutive segments, pick the best
parameters on each one, then measure that choice on the *next* segment, which the
selection never saw. Only the out-of-sample results count.

```bash
python3 -m tradebot walkforward -s slow_trend --files "data/*_1h.csv" \
    --param period=100,200,400,600 band_pct=0.0,0.02,0.05 --folds 6
```

**18 folds across BTC, ETH and SOL:**

| | |
|---|---|
| in-sample, as picked | **+14.2pp** — what fitting the grid promised |
| out-of-sample, actual | **+2.9pp** — what it delivered next |
| lost to overfitting | **11.3pp** |
| folds beating buy-and-hold | **10 of 18** |
| **drawdown cut, median** | **+20.0 points** |

**Eighty percent of the apparent edge evaporated.** What was left — +2.9pp mean, 10 of
18 folds — is a coin flip, indistinguishable from zero. The return edge was never
there; it was the grid memorising noise.

**The drawdown reduction survived.** +20 points, median, out of sample, on data the
parameter choice never saw. That is now the only claim in this repository that has
withstood every test applied to it: multiple markets, three timeframes, twelve
parameter settings, and finally out-of-sample validation.

### So, honestly

After a decade of stocks, 2.3 years of hourly crypto, 208 days of 15-minute crypto,
54 strategy runs, a parameter sweep and a walk-forward:

- **No strategy here reliably beats buying and holding.** Two of 54 in-sample runs did,
  and the walk-forward shows why not to trust that.
- **Cost drag is the mechanism.** 0.1%/year for holding against 24–148%/year for the
  active strategies. No entry signal outruns that.
- **One effect is real:** trend-following reduces drawdowns, substantially and
  repeatably, at the price of large underperformance in rising markets. It is
  insurance, and it should be bought or declined as insurance — not as an edge.

That is a genuinely useful answer, and it is not the one anybody wants.

## Seeing the trades

Every number above can be checked, trade by trade:

```bash
python3 -m tradebot trades --symbol BA --strategy never_lose
python3 -m tradebot trades --symbol SPY --strategy ema_cross --limit 0     # all of them
python3 -m tradebot trades --symbol SPY --strategy ema_cross --csv-out my_trades.csv
```

```
     #  opened     closed       days          qty      entry       exit      gross    costs        net     balance  reason
  ------------------------------------------------------------------------------------------------------------------------
     1  2016-08-30 2016-10-05     36      81.6026     122.58     122.63     +10.01    10.01      0.00 $ 10,000.00  take profit
     2  2016-10-06 2016-10-10      4      80.6100     124.09     124.14     +10.01    10.01     -0.00 $ 10,000.00  take profit
     3  2016-10-11 2016-10-14      3      80.5443     124.19     124.24     +10.01    10.01      0.00 $ 10,000.00  take profit
     4  2016-10-17 2016-10-18      1      81.4131     122.87     124.20    +114.61    10.06 +   104.55 $ 10,104.55  take profit

  128 trades: 97 winners, 31 losers (75.8% win rate)
  Gross $1,460.22  -  costs $1,766.89  =  net $-306.67
  Longest hold: 2,736 days (2019-03-01 to 2026-08-27), net $-10,453.25
```

That is `never_lose` on Boeing, and it shows in four rows what a summary table cannot:
the first three "winners" netted **exactly £0.00** — a $10.01 gain against $10.01 of
costs — and the last line is a single position held **seven and a half years** that
lost more than the entire starting account. Winners are green, losers red, and
`--csv-out` writes the lot for a spreadsheet.

Wins and losses are separated into gross, costs and net on every row, so a trade that
looks profitable but isn't cannot hide.

**Live and paper runs** append each closed trade to `state/trades.csv` the moment it
closes, so the history survives a restart and can be watched while the bot runs:

```bash
python3 -m tradebot paper &
tail -f state/trades.csv
```

## What you get

```
python3 -m tradebot demo           the cost arithmetic, run on real data
python3 -m tradebot study          stocks or local bar files, every strategy vs buy-and-hold
python3 -m tradebot trades         every trade a strategy made, with a CSV export
python3 -m tradebot sweep          how much of a result is real vs cherry-picked
python3 -m tradebot walkforward    would the choice have worked on unseen data?
python3 -m tradebot status         check on a running session without stopping it
python3 -m tradebot report         end-of-run verdict for one or more sessions
python3 -m tradebot strategies     list available strategies
python3 -m tradebot fetch          download history to CSV
python3 -m tradebot backtest       replay a strategy over history
python3 -m tradebot paper          run automated: live data, simulated money
python3 -m tradebot preflight      are you actually ready for real money?
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
| `study.py` | Multi-symbol, multi-year comparison against buy-and-hold |
| `tradelog.py` | The trade log: terminal table, CSV export, live append |
| `sweep.py` | Parameter grids, so a lucky cell cannot pass as a finding |
| `walkforward.py` | Out-of-sample validation: the check on every other number here |
| `report.py` | End-of-run verdict, benchmarked against holding over the same window |
| `preflight.py` | Readiness checks that must pass before risking real money |
| `live.py` | The unattended runner, with state that survives restarts |
| `brokers/` | Paper execution, and a hard-gated Crypto.com live adapter |
| `feeds/` | Crypto.com and Yahoo data, CSV files, and a synthetic generator |
| `strategies/` | Six strategies: a benchmark, two that fail instructively, and one that insures |

Run the tests with `python3 -m unittest discover -s tests -t .` — there are 287, and
they cover the accounting, the risk limits, and the ways backtesters usually lie.
Two files do more than check behaviour that was designed. `tests/test_golden.py` pins
every strategy's end-to-end result to the penny, so a change to fill pricing or bracket
logic cannot silently move every number in this file while the unit tests stay green.
`tests/test_adversarial.py` runs hostile inputs — a one-bar series, a 99% crash, a £1
account, a flat fee on a £200 balance — against every registered strategy, asserting
invariants no strategy may break. It found a real one: the engine sized positions from
equity and then charged the fee, spending money the account did not have.
`tests/test_broker_signing.py` pins the live request byte for byte, because a signature
is the one thing here that cannot be checked by running it — a wrong one comes back as
a 401 indistinguishable from an expired key. It proves the implementation matches the
venue's published algorithm; it does not prove the algorithm is current, and none of it
has been checked against a funded account.

---

## Running it for days or weeks

This is the workflow the whole repository points at, so it is a single command.
Paper trading places no orders and needs no API keys.

```bash
./scripts/paper-run.sh start     # runs every config in configs/, restarts on crash
./scripts/paper-run.sh status    # what each session is doing right now
./scripts/paper-run.sh report    # the verdict so far, against buy-and-hold
./scripts/paper-run.sh stop
```

Three sessions ship in `configs/`: `buy_and_hold` as the benchmark, `slow_trend` (the
only strategy here with any validated effect), and `never_lose`. Running the benchmark
alongside is the point — without it a return is unreadable.

For a run that survives reboots, `scripts/tradebot-paper.service` (systemd) and
`scripts/com.tradebot.paper.plist` (launchd) are ready to edit. State is saved after
every bar, so a restart resumes rather than starting over — a crash costs one bar, not
the session.

**On a laptop:** a sleeping machine stops the bot. It will resume on wake, and the gap
in its data is real, so prefer something that stays awake for a run you intend to
judge.

### Reading the result

```
  Session report - 14.2 days, 20,412 bars

  strategy              final    return   vs hold  trades   wins     costs   cost/yr
  buy_and_hold     £ 1,022.40    +2.24%         -       1      - £    1.00      0.3%
  slow_trend       £ 1,008.10    +0.81%    -1.43p       4    3/4 £    4.10      1.1%
  never_lose       £   995.08    -0.49%    -2.73p      31   31/31 £   26.62     68%
```

The `vs hold` and `cost/yr` columns are the ones to read. **Returns need a month at
minimum and are thin evidence even then** — the backtests in this file are a better
guide to whether a strategy works. **Cost drag is reliable within days**, because it
does not depend on which way the market went, and it is what actually decides most of
these outcomes.

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

Leave the paper run going for weeks — see **Running it for days or weeks** above for
the supervised runner and the systemd/launchd units. Check on it any time with
`python3 -m tradebot status`, which reads only the files the runner writes and is safe
to call against a live bot.

### How a backtest avoids lying to you

Most backtests are optimistic in four specific ways. This one is not:

- **Look-ahead.** A decision made at a bar's close is filled at the *next* bar's
  open. You cannot trade on a price before you have seen it.
- **Costs.** Spread, slippage and commission are charged on every fill, and reported
  separately from the market move so you can see exactly what the venue took.
- **Ambiguous bars.** If a bar's range touched both your stop and your target, the
  engine assumes the stop hit first. A candle cannot say which came first, so it
  takes the pessimistic reading.
- **Gaps.** A stop is a market order, so a bar that opens below it sells into the gap
  at the open, worse than the stop price. A take-profit is a limit order, so a bar
  that opens above it fills at the open, better than the target. Filling both at the
  trigger price is the obvious implementation and it quietly distorts any strategy
  whose brackets sit inside a normal day's range.
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
| `max_trades_per_day` | 20 | Orders, not round trips — both legs count, so 10 complete trades |
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

Before any of the mechanics, run:

```bash
python3 -m tradebot preflight
```

The four gates below stop an *accident*. They cannot tell you whether going live is a
good idea, because a config flag knows nothing about whether the strategy works.
`preflight` asks the substantive questions instead, from evidence on disk: have you
paper traded, for how long, did it make money, and does the strategy beat simply
holding? It blocks on the answers, and it will not congratulate you — passing only
means the risk is informed, never that you will profit.

Live trading is off by default and takes four independent steps to arm. This is
deliberate — each one is a place to stop and reconsider.

1. `enabled = true` in `[live]`
2. `CRYPTOCOM_API_KEY` and `CRYPTOCOM_API_SECRET` in your **environment**, never in
   the config file (which can end up in git)
3. `dry_run = false` in `[live]` — until then, orders are logged in full but not sent
4. `--yes-really-trade-live` on the command line, on every run

Plus `max_order_notional`, a hard client-side cap on any single order, enforced in
the broker so nothing can route around it. Start at £10.

A fifth requirement that is not a flag: **run it somewhere that stays up.** If the
machine stops, the bot stops — holding a position it can no longer manage or exit. A
laptop that sleeps and an ephemeral cloud session both fail this.

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
system are for, and exactly what the ten-year table above measures.

And if after all that the honest answer is that a low-cost index fund held for a
decade beat everything here by 200 percentage points, that is a real answer worth
having. It cost you a weekend to find out rather than a decade of fees.
