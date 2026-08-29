# tradebot — notes for whoever picks this up

An automated trading system, written to be honest about costs. Python 3.11+, standard
library only — no packages, no build step, no network needed except for live data.

The owner runs **Windows**. Use `py`, not `python3`. `scripts\paper-run.bat` is the
supervisor; `scripts/paper-run.sh` is the Unix twin and will not run in cmd.

## What was measured, so it is not re-litigated

Ten years of US stocks, several years of crypto, at three timeframes, checked with
walk-forward validation on markets held back for the purpose:

- **No strategy here reliably beats buying and holding.** Out of sample it lost to
  holding in the large majority of tests, in every market family tried.
- **Cost drag is the mechanism.** 0.1%/year to hold against 24–148%/year for the fast
  strategies. No entry signal outruns that.
- **One effect is real and survived every test:** these strategies cut drawdowns, by a
  lot. `vol_target` roughly halves the worst fall. That makes this insurance, not an
  edge.
- **Diversify first, then size by risk** is the best risk-adjusted combination found —
  better Sharpe than SPY in both halves of the decade, at a third of the drawdown, and
  still less money than simply holding the index.

Do not "improve" the returns by tuning parameters on the backtest. That was tried, at
length; walk-forward showed about 80% of the apparent gain was noise. If a change makes
the backtest look better, the burden is to show it survives `tradebot walkforward` on
data the choice never saw.

## The rules this codebase holds itself to

1. **Never flatter a result.** Costs are charged on every fill, decisions execute on the
   *next* bar, ambiguous bars resolve against us, and both benchmarks — the same asset
   held, and an index fund held — are always shown.
2. **A test that cannot fail is worse than no test.** `./scripts/mutation-sweep.sh`
   breaks the code on purpose 54 ways and checks the suite notices. Run it after
   changing the engine, the cost model, or anything under analysis. All 54 must be
   caught.
3. **The golden table pins every strategy's end-to-end result.** If `tests/test_golden.py`
   moves, either it is a real regression or the change was deliberate — never update the
   numbers just to get green.
4. **Live-money paths are gated.** Four independent gates, credentials only from the
   environment, and a client-side order cap. Do not loosen any of them.

## Commands

```bat
py -m unittest discover -s tests -t .     :: 440 tests, ~4s
py -m tradebot demo                        :: the cost arithmetic, offline
scripts\paper-run.bat start                :: paper trade every config
scripts\paper-run.bat fast                 :: same, on 1-minute bars, to see it work
scripts\paper-run.bat watch                :: live dashboard at 127.0.0.1:8765
scripts\paper-run.bat logs                 :: why a session looks idle
scripts\paper-run.bat stop
```

Research: `study`, `sweep`, `walkforward`, `basket`, `trades`, `report`, `preflight`.

## Traps already hit here, so they are not hit twice

- **A Windows path in a TOML string is invalid TOML.** `\U` in `C:\Users` starts a
  Unicode escape. Use forward slashes. The loader explains this now.
- **cmd does not expand `*`.** The CLI expands globs itself; keep it that way.
- **The engine's warm-up is separate from the live runner's.** Telling the engine about
  warm-up is what stops a 200-day strategy idling for 200 live days after a restart.
- **Never mark a position at the last closed trade's price.** It can be stale or from
  another run. Use the last price the session actually saw, or show nothing.

## What not to do

Do not offer to trade the owner's money, and do not build anything that places live
orders without them explicitly arming all four gates. The honest advice already given,
and worth repeating if asked, is that a low-cost index fund held for a decade beat
everything in this repository.
