#!/usr/bin/env bash
#
# Supervise one or more paper-trading sessions for days or weeks.
#
# Each strategy runs in its own process against its own config, state file and trade
# log, and is restarted if it dies. Restarts are safe: the runner saves state after
# every bar and resumes from it, so a crash costs at most one bar rather than the
# session's history.
#
#   ./scripts/paper-run.sh start          # start every config in configs/
#   ./scripts/paper-run.sh status         # what each one is doing
#   ./scripts/paper-run.sh report         # the verdict so far, vs buy-and-hold
#   ./scripts/paper-run.sh stop
#
# Leave it running for a month before drawing any conclusion about returns. Cost
# figures become reliable much sooner - within days - because they do not depend on
# which way the market went.

set -euo pipefail
cd "$(dirname "$0")/.."

CONFIG_DIR="${TRADEBOT_CONFIGS:-configs}"
RUN_DIR="${TRADEBOT_RUN:-state}"
PYTHON="${PYTHON:-python3}"

mkdir -p "$RUN_DIR"

configs() {
  shopt -s nullglob
  local found=("$CONFIG_DIR"/*.toml)
  if [ ${#found[@]} -eq 0 ]; then
    echo "no configs in $CONFIG_DIR/ - copy config.example.toml in and edit it" >&2
    exit 1
  fi
  printf '%s\n' "${found[@]}"
}

supervise() {
  local cfg="$1" name
  name="$(basename "$cfg" .toml)"
  # Restart on exit, but back off so a config that cannot start does not spin.
  local delay=5
  while true; do
    "$PYTHON" -m tradebot --config "$cfg" paper >> "$RUN_DIR/${name}_session.log" 2>&1 || true
    [ -f "$RUN_DIR/${name}.stop" ] && break
    echo "$(date -u +%FT%TZ) $name exited, restarting in ${delay}s" >> "$RUN_DIR/${name}_session.log"
    sleep "$delay"
    delay=$(( delay < 300 ? delay * 2 : 300 ))
  done
}

case "${1:-start}" in
  start)
    while read -r cfg; do
      name="$(basename "$cfg" .toml)"
      rm -f "$RUN_DIR/${name}.stop"
      if [ -f "$RUN_DIR/${name}.pid" ] && kill -0 "$(cat "$RUN_DIR/${name}.pid")" 2>/dev/null; then
        echo "  $name already running (pid $(cat "$RUN_DIR/${name}.pid"))"
        continue
      fi
      # Detach completely. A backgrounded child that inherits stdout holds the
      # parent's pipe open, so `paper-run.sh start | tail` would hang forever waiting
      # on processes designed never to exit.
      supervise "$cfg" </dev/null >/dev/null 2>&1 &
      echo $! > "$RUN_DIR/${name}.pid"
      echo "  started $name (pid $!)"
      disown 2>/dev/null || true
    done < <(configs)
    echo
    echo "  Running. Check in with:  ./scripts/paper-run.sh status"
    echo "  Verdict so far:          ./scripts/paper-run.sh report"
    ;;
  stop)
    while read -r cfg; do
      name="$(basename "$cfg" .toml)"
      # Order matters. Drop the stop flag first so the supervisor will not restart,
      # then signal the worker by its exact config so a grandchild cannot outlive its
      # supervisor, then retire the supervisor itself.
      touch "$RUN_DIR/${name}.stop"
      pkill -TERM -f -- "-m tradebot --config $cfg paper" 2>/dev/null || true
      if [ -f "$RUN_DIR/${name}.pid" ]; then
        kill -TERM "$(cat "$RUN_DIR/${name}.pid")" 2>/dev/null || true
        rm -f "$RUN_DIR/${name}.pid"
      fi
      # Give it a moment to save state, then insist.
      sleep 1
      pkill -KILL -f -- "-m tradebot --config $cfg paper" 2>/dev/null || true
      echo "  stopped $name"
    done < <(configs)
    ;;
  status)
    while read -r cfg; do "$PYTHON" -m tradebot --config "$cfg" status || true; done < <(configs)
    ;;
  report)
    # shellcheck disable=SC2046
    "$PYTHON" -m tradebot report $(configs | tr '\n' ' ')
    ;;
  *)
    echo "usage: $0 {start|stop|status|report}" >&2
    exit 2
    ;;
esac
