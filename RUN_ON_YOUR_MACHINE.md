# Running this on your own machine

Everything in this repository has been developed and tested in a cloud container that
gets wiped when the session ends. That is why there is no live paper-trading record in
here: every session started during development died with the machine it ran on.

A live run needs somewhere that stays on. That means your machine, and these are the
commands.

## Windows: the short version

Everything below assumes a Unix shell. On Windows the command is `py`, not `python3`,
`cmd` does not expand `*` for you (the program does it instead), and the `.sh` scripts
will not run — use `scripts\paper-run.bat`.

```bat
cd %USERPROFILE%
git clone https://github.com/ReeceMcAllion1/tradingapp.git
cd tradingapp
git checkout claude/automated-trading-system-23jrwc

py -m unittest discover -s tests -t .     :: check it works
scripts\paper-run.bat start               :: start paper trading
scripts\paper-run.bat watch               :: open the dashboard
scripts\paper-run.bat stop                :: stop everything
```

### Editing config.toml on Windows

If you put a Windows path in, use forward slashes:

```toml
state_file = "C:/Users/you/tradingapp/state/live_state.json"
```

They work fine on Windows. A raw `C:\Users\...` is not valid TOML — the backslash
starts an escape sequence — and the parser's own complaint ("Invalid hex value") gives
no hint of that, so the loader now explains it instead.

No git? Download the branch as a ZIP from GitHub — the green **Code** button, then
**Download ZIP** — unzip it, and `cd` into the unzipped folder before running anything.
`No module named tradebot` always means you are in the wrong folder.

## What you need

Python 3.11 or newer. Nothing else — no packages to install, no API keys, no account.
Paper trading places no orders and needs no credentials.

```bash
python3 --version      # must be 3.11+
```

## Get it

```bash
git clone https://github.com/ReeceMcAllion1/tradingapp.git
cd tradingapp
git checkout claude/automated-trading-system-23jrwc
```

## Check it works

```bash
python3 -m unittest discover -s tests -t .    # 387 tests, ~3 seconds
python3 -m tradebot demo                      # the cost arithmetic, offline
```

## Start paper trading

```bash
./scripts/paper-run.sh start
```

That runs every config in `configs/` at once, each on live market data with simulated
money, restarting any session that crashes. It detaches, so you can close the terminal.

```bash
./scripts/paper-run.sh status     # what each session is doing right now
./scripts/paper-run.sh report     # the verdict so far, against buy-and-hold
./scripts/paper-run.sh stop       # stop them all
```

## Watch it live, in a browser

```bash
python3 -m tradebot dashboard configs/*.toml
```

Opens `http://127.0.0.1:8765` and refreshes itself every five seconds: position, equity,
fees, the live stop, and every completed round trip with the reason the strategy gave.
Leave the tab open. Closing it does not stop the bot; stopping the dashboard does not
either — it only ever reads.

It listens on localhost only, on purpose: the page shows your positions and balances,
and that should not be readable by the rest of your network. To watch from another
machine, tunnel it rather than exposing it:

```bash
ssh -N -L 8765:127.0.0.1:8765 you@your-box
```

Or, if you prefer the terminal:

```bash
tail -f state/*_trades.csv        # every closed round trip as it happens
tail -f state/*.log               # every bar, every decision
```

`report` is the one to check after a few days. It marks the open position to market and
compares the session against simply having held the same asset over the same hours —
which is the only comparison that has ever mattered in this project.

## How long before it means anything

Days for the machinery, months for the result. `python3 -m tradebot preflight` will
refuse to bless a live run on less than 30 trades over 30 days, and that is a floor
rather than a target. A fortnight of paper trading tells you the software works. It
tells you almost nothing about whether the strategy does.

## Before you ever consider real money

Read `preflight` and believe it:

```bash
python3 -m tradebot preflight
```

It checks the things that actually decide this: whether there is enough paper history,
whether that history made money, whether it beat buy-and-hold, whether the cost drag is
survivable, and whether the machine you are on will still be running tomorrow. It cannot
stop you. It reports honestly and lets you decide.

The honest summary of ten years of testing is in the README, and it has not changed:
nothing here reliably beats buying and holding. What it does do is lose less in a crash.
