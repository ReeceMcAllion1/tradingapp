#!/usr/bin/env bash
#
# Break the code on purpose, and check the tests notice.
#
# A passing test suite proves the tests run. It does not prove they would fail if the
# code were wrong - and a test that cannot fail is worse than no test, because it reads
# like coverage. This script introduces one real defect at a time and asserts the suite
# rejects it.
#
# Every mutation below is a plausible bug, not a random character swap: a cost that
# stops being charged, a limit that stops binding, a validator that stops validating.
# Several were found this way. The two worth naming: walk-forward could be pointed at
# its own training data, and the parameter sweep's mean could report its best cell -
# each would have turned the analysis that underpins every honest claim here into a
# flattering one, silently, with the suite green.
#
# Usage:  ./scripts/mutation-sweep.sh
# Exit:   0 if every mutation is caught, 1 if any survives.

set -uo pipefail
cd "$(dirname "$0")/.."

# This script edits your source files and puts them back. Two copies running at once
# will race on that - one takes its backup while the other has a mutation applied, and
# restoring leaves real damage on disk. It happened during development, which is how
# this lock came to exist. Refuse rather than corrupt.
LOCK=".mutation-sweep.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
    echo "Another mutation sweep is running (or one crashed and left $LOCK behind)." >&2
    echo "Wait for it to finish, or remove $LOCK if you are sure nothing is running." >&2
    exit 1
fi

if ! git diff --quiet -- tradebot 2>/dev/null; then
    echo "You have uncommitted changes under tradebot/." >&2
    echo "This script rewrites those files and restores them from a copy - commit or" >&2
    echo "stash first, so a crash cannot cost you work." >&2
    rmdir "$LOCK"
    exit 1
fi

if ! python3 -m unittest discover -s tests -t . >/dev/null 2>&1; then
    echo "The suite fails before any mutation. Fix that first." >&2
    rmdir "$LOCK"
    exit 1
fi

BACKUP="$(mktemp -d)/tradebot"
cp -r tradebot "$BACKUP"
restore() { rm -rf tradebot && cp -r "$BACKUP" tradebot; }
cleanup() {
    restore
    rmdir "$LOCK" 2>/dev/null
}
# Covers the interrupt too: a Ctrl-C partway through must not leave a mutation behind.
trap cleanup EXIT INT TERM

caught=0
escaped=0
escaped_names=()

mutate() {
    local file="$1" old="$2" new="$3" label="$4"

    python3 - "$file" "$old" "$new" <<'PY'
import sys
path, old, new = sys.argv[1:4]
source = open(path).read()
if old not in source:
    sys.exit(2)
open(path, "w").write(source.replace(old, new, 1))
PY
    case $? in
        2)  printf '  \033[33mSTALE\033[0m    %s\n' "$label"
            printf '           (the code moved; update this mutation)\n'
            escaped=$((escaped + 1)); escaped_names+=("$label (stale)")
            restore; return ;;
    esac

    if python3 -m unittest discover -s tests -t . >/dev/null 2>&1; then
        printf '  \033[31mESCAPED\033[0m  %s\n' "$label"
        escaped=$((escaped + 1)); escaped_names+=("$label")
    else
        printf '  caught   %s\n' "$label"
        caught=$((caught + 1))
    fi
    restore
}

echo
echo "  Costs and accounting"
mutate tradebot/costs.py \
    'return reference_price * (1.0 + adverse)' 'return reference_price' \
    'fills stop paying the spread'
mutate tradebot/costs.py \
    'return abs(notional) * rate * BPS + self.flat_fee' 'return 0.0' \
    'commission becomes free'
mutate tradebot/costs.py \
    'price = entry_fill_price * (1.0 + fee_rate) / ((1.0 - fee_rate) * (1.0 - adverse))' \
    'price = entry_fill_price' \
    'break-even ignores every cost'
mutate tradebot/portfolio.py \
    'self.cash -= fill.fee' 'pass' \
    'fees never leave the account'
mutate tradebot/portfolio.py \
    'self.cash -= fill.signed_qty * fill.price' 'pass' \
    'trades stop costing anything'

echo
echo "  Engine"
mutate tradebot/engine.py \
    'fills += self._check_brackets(candle)' 'pass' \
    'stops and targets never fire'
mutate tradebot/engine.py \
    'self.pending = decision' 'self.pending = None' \
    'decisions never reach the market'
mutate tradebot/engine.py \
    'if self.bars_seen <= self.strategy.warmup:' 'if False:' \
    'trades on half-formed indicators'
mutate tradebot/engine.py \
    'if delta > 0:' 'if False:' \
    'positions funded by an overdraft'
mutate tradebot/engine.py \
    'if drift < self.execution.rebalance_threshold:' 'if False:' \
    'rebalances on every bar'
mutate tradebot/engine.py \
    'return one_way_cost <= moved * self.execution.max_resize_cost_share' 'return True' \
    'pays any price to correct a drift'
mutate tradebot/engine.py \
    'self.risk.record_order()' 'pass' \
    'orders stop counting against the daily cap'

echo
echo "  Risk limits"
mutate tradebot/risk.py \
    'if drawdown >= self.limits.max_drawdown_pct:' 'if False:' \
    'kill switch disabled'
mutate tradebot/risk.py \
    'if day_loss >= self.limits.max_daily_loss_pct:' 'if False:' \
    'daily loss limit disabled'
mutate tradebot/risk.py \
    'capped = max(-self.limits.max_position_pct, min(self.limits.max_position_pct, requested))' \
    'capped = requested' \
    'position cap ignored'
mutate tradebot/risk.py \
    'if not self.limits.allow_short and requested < 0:' 'if False:' \
    'shorting allowed regardless of the setting'

echo
echo "  What gets reported"
mutate tradebot/metrics.py \
    'return worst * 100.0' 'return 0.0' \
    'drawdown always reported as zero'
mutate tradebot/metrics.py \
    'return (mean / stdev) * math.sqrt(bars_per_year)' 'return 0.0' \
    'Sharpe always reported as zero'
mutate tradebot/metrics.py \
    'return self.total_costs / self.starting_equity * 100.0' 'return 0.0' \
    'cost drag always reported as zero'
mutate tradebot/report.py \
    'return self.return_pct - self.benchmark_return_pct' 'return self.return_pct' \
    'the "vs hold" gap ignores the benchmark'
mutate tradebot/tradelog.py \
    '"net": round(trade.net_pnl, 2),' '"net": round(trade.gross_pnl, 2),' \
    'the trade log shows gross profit as net'
mutate tradebot/tradelog.py \
    '"costs": round(trade.total_cost, 2),' '"costs": 0.0,' \
    'the trade log hides costs'

echo
echo "  Analysis - the numbers every claim rests on"
mutate tradebot/walkforward.py \
    'train, test = chunks[i], chunks[i + 1]' 'train, test = chunks[i], chunks[i]' \
    'walk-forward tests on its own training data'
mutate tradebot/walkforward.py \
    'best_gap, chosen = max(scored, key=lambda pair: pair[0])' \
    'best_gap, chosen = min(scored, key=lambda pair: pair[0])' \
    'walk-forward carries forward the worst parameters'
mutate tradebot/walkforward.py \
    'out_of_sample_drawdown_cut=test_bench.max_drawdown_pct - tested.max_drawdown_pct,' \
    'out_of_sample_drawdown_cut=0.0,' \
    'walk-forward stops measuring the drawdown cut'
mutate tradebot/sweep.py \
    'return sum(gaps) / len(gaps) if gaps else 0.0' 'return max(gaps) if gaps else 0.0' \
    'the sweep reports its best cell as the mean'
mutate tradebot/sweep.py \
    'return self.metrics.total_return_pct - self.benchmark.total_return_pct' \
    'return self.metrics.total_return_pct' \
    'the sweep gap ignores the benchmark'

echo
echo "  Live trading"
mutate tradebot/brokers/cryptocom.py \
    '    if isinstance(obj, float):' '    if False:' \
    'floats are signed, and hash differently at each end'
mutate tradebot/brokers/cryptocom.py \
    'qty = floor_to_decimals(capped, self.qty_decimals)' \
    'qty = round(capped, self.qty_decimals)' \
    'the order cap rounds up past itself'
mutate tradebot/live.py \
    'if candle.ts <= self._last_bar_ts:' 'if False:' \
    'the same bar is processed twice'
mutate tradebot/engine.py \
    'touched = candle.low <= through if buying else candle.high >= through' \
    'touched = True' \
    'limit orders always fill, so the miss is never paid for'
mutate tradebot/engine.py \
    'else order.limit_price * (1.0 + self.execution.maker_queue_bps * 1e-4)' \
    'else order.limit_price' \
    'the queue assumption stops being applied to sells'
mutate tradebot/brokers/paper.py \
    'fee=self.costs.fee(qty * price, liquidity),' \
    'fee=self.costs.fee(qty * price, Liquidity.MAKER),' \
    'every fill is billed at the cheaper maker rate'
mutate tradebot/engine.py \
    'if decision.is_hold and decision.stop_loss is None and decision.take_profit is None:' \
    'if False:' \
    'holding silently drops its own stop-loss'

echo
echo "  Strategies and feeds"
mutate tradebot/strategies/trend.py \
    'self._trail = candidate if self._trail is None else max(self._trail, candidate)' \
    'self._trail = candidate' \
    'the trailing stop follows price down'
mutate tradebot/strategies/never_lose.py \
    'target = ctx.costs.net_breakeven_exit(ctx.avg_price, qty)' \
    'target = ctx.avg_price' \
    'never_lose exits at the entry price, a guaranteed loss'
mutate tradebot/strategies/vol_target.py \
    'weight = min(self.max_weight, self.target_vol / annualised)' \
    'weight = self.max_weight' \
    'volatility sizing stops sizing by volatility'
mutate tradebot/strategies/vol_target.py \
    'rungs = math.floor(weight / self.step)' \
    'rungs = weight / self.step' \
    'the position stops being quantised, and chases noise'
mutate tradebot/strategies/vol_target.py \
    'annualised = stdev * math.sqrt(self._bars_per_year)' \
    'annualised = stdev' \
    'volatility is not annualised, so the target means nothing'
mutate tradebot/feeds/base.py \
    'ordered = sorted(candles, key=lambda c: c.ts)' 'ordered = candles' \
    'out-of-order bars are fed to the strategy'

echo
echo "  Preflight"
mutate tradebot/preflight.py \
    'if rows and net <= 0:' 'if False:' \
    'losing paper trading stops blocking live'
mutate tradebot/preflight.py \
    'ok = annual_cost_drag_pct <= MAX_ANNUAL_COST_DRAG_PCT' 'ok = True' \
    'ruinous cost drag stops blocking live'

echo
echo "  ------------------------------------------------------------"
if [ "$escaped" -eq 0 ]; then
    printf "  All %d mutations caught.\n" "$caught"
    echo "  ------------------------------------------------------------"
    echo
    exit 0
fi
printf "  %d caught, \033[31m%d escaped\033[0m:\n" "$caught" "$escaped"
for name in "${escaped_names[@]}"; do echo "    - $name"; done
echo
echo "  An escaped mutation is a real gap: that bug could ship today."
echo "  Write the test that fails, then re-run. A stale one means the code"
echo "  moved - point the mutation at where the behaviour lives now."
echo "  ------------------------------------------------------------"
echo
exit 1
