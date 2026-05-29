#!/usr/bin/env bash
# Gallery uptime watchdog.
#
# Every WATCH_INTERVAL seconds:
#   • HTTP-ping /api/health/. If 3 consecutive pings fail (no response, 500,
#     or timeout >5s), force-recycle the gunicorn worker by sending SIGTERM
#     to it. The supervised master spawns a fresh worker automatically.
#   • Check the worker's RSS. If it crosses RSS_LIMIT_MB, do a graceful
#     recycle via SIGTERM before macOS OOM-kills the whole tree.
#
# This is the last-resort safety net for *unknown-unknown* failures: thread
# deadlocks the supervisor + max-requests cap can't catch.
#
# Run as:
#   nohup ./watchdog.sh </dev/null >> watchdog.log 2>&1 &
#   disown
#
# Or it's auto-started by start_gunicorn_detached.sh.

set -u
cd "$(dirname "$0")"

WATCH_INTERVAL="${WATCHDOG_INTERVAL:-60}"      # seconds between checks
HEALTH_URL="${WATCHDOG_HEALTH_URL:-http://127.0.0.1:8765/api/health/}"
HEALTH_TIMEOUT="${WATCHDOG_HEALTH_TIMEOUT:-5}" # curl --max-time
FAIL_THRESHOLD="${WATCHDOG_FAIL_THRESHOLD:-2}" # consecutive misses → recycle (2 = ~2 min)
RSS_LIMIT_MB="${WATCHDOG_RSS_LIMIT_MB:-3500}"  # graceful recycle above this
LOG_PREFIX="[watchdog]"

# Avoid two watchdogs running. Write a PID file; refuse to start if a
# prior watchdog is still alive.
PIDFILE=".watchdog.pid"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "$LOG_PREFIX another watchdog already running (pid $(cat "$PIDFILE")) — exiting"
    exit 0
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

ts() { date "+%Y-%m-%d %H:%M:%S"; }

# Find the gunicorn *worker* PID (not the master). Master has a single
# gunicorn child; the worker is the one with --bind in its cmdline AND
# whose parent is itself a gunicorn process.
find_worker_pid() {
    # Look for gunicorn processes whose PPID is also a gunicorn process.
    ps -eo pid,ppid,command | awk '/[g]unicorn .*djangoscrap.wsgi/ {print $1, $2}' | while read pid ppid; do
        parent_cmd=$(ps -o command= -p "$ppid" 2>/dev/null || echo "")
        if echo "$parent_cmd" | grep -q "gunicorn.*djangoscrap.wsgi"; then
            echo "$pid"
            return
        fi
    done | head -1
}

recycle_worker() {
    # Before killing the worker, snapshot its Python thread stacks via py-spy
    # to ./pyspy_dumps/. That gives us a forensic record of what was actually
    # deadlocked — without it we have no way to fix the underlying cause and
    # the same hang recurs every ~20 minutes.
    # Then SIGTERM. If still alive after KILL_ESCALATE_S seconds, SIGKILL
    # (genuinely deadlocked workers don't respond to SIGTERM because Python's
    # signal handlers can't run while threads are stuck in blocking syscalls).
    local why="$1"
    local pid
    pid=$(find_worker_pid)
    if [ -z "$pid" ]; then
        echo "$LOG_PREFIX $(ts) wanted to recycle ($why) but no worker PID found"
        return
    fi

    # py-spy dump — best-effort, never blocks recycling.
    local pyspy_bin="${PYSPY_BIN:-/opt/homebrew/bin/py-spy}"
    if [ -x "$pyspy_bin" ]; then
        local dumps_dir="$(pwd)/pyspy_dumps"
        mkdir -p "$dumps_dir"
        local stamp; stamp=$(date "+%Y%m%d_%H%M%S")
        local dump_file="$dumps_dir/dump_pid${pid}_${stamp}_${why}.txt"
        echo "$LOG_PREFIX $(ts) py-spy dump → $dump_file"
        # --nonblocking so we don't pause the worker we're about to kill anyway.
        # Run as the same user that owns the gunicorn process (us) — no sudo needed.
        timeout 5 "$pyspy_bin" dump --pid "$pid" --nonblocking > "$dump_file" 2>&1 || \
            echo "$LOG_PREFIX py-spy dump failed (rc=$?) — check $dump_file"
    fi

    echo "$LOG_PREFIX $(ts) RECYCLING worker PID $pid — reason: $why  (SIGTERM)"
    kill -TERM "$pid" 2>/dev/null || echo "$LOG_PREFIX SIGTERM to $pid failed"

    # Escalation watcher: SIGKILL after the graceful timeout if the worker
    # didn't exit. Run in a subshell so it doesn't block the main loop.
    (
        local escalate_s="${WATCHDOG_KILL_ESCALATE_S:-15}"
        sleep "$escalate_s"
        if kill -0 "$pid" 2>/dev/null; then
            echo "$LOG_PREFIX $(ts) worker $pid still alive ${escalate_s}s after SIGTERM — escalating to SIGKILL"
            kill -KILL "$pid" 2>/dev/null || echo "$LOG_PREFIX SIGKILL to $pid failed"
        fi
    ) &
}

consecutive_fails=0
echo "$LOG_PREFIX $(ts) started (interval=${WATCH_INTERVAL}s url=$HEALTH_URL rss_limit=${RSS_LIMIT_MB}MB)"

while true; do
    sleep "$WATCH_INTERVAL"

    # ── 1. Health ping ─────────────────────────────────────────────────
    code=$(curl -s -o /dev/null --max-time "$HEALTH_TIMEOUT" -w "%{http_code}" "$HEALTH_URL" 2>/dev/null || echo 000)
    if [ "$code" = "200" ]; then
        if [ "$consecutive_fails" -gt 0 ]; then
            echo "$LOG_PREFIX $(ts) health OK (recovered after $consecutive_fails miss)"
        fi
        consecutive_fails=0
    else
        consecutive_fails=$((consecutive_fails + 1))
        echo "$LOG_PREFIX $(ts) health FAIL #$consecutive_fails (code=$code)"
        if [ "$consecutive_fails" -ge "$FAIL_THRESHOLD" ]; then
            recycle_worker "health-fail-$consecutive_fails"
            consecutive_fails=0
            # Give the new worker a chance to come up before the next check.
            sleep 5
            continue
        fi
    fi

    # ── 2. RSS check ───────────────────────────────────────────────────
    pid=$(find_worker_pid)
    if [ -n "$pid" ]; then
        # macOS ps gives RSS in KB.
        rss_kb=$(ps -o rss= -p "$pid" 2>/dev/null | awk '{print $1}')
        if [ -n "$rss_kb" ]; then
            rss_mb=$((rss_kb / 1024))
            if [ "$rss_mb" -ge "$RSS_LIMIT_MB" ]; then
                recycle_worker "rss=${rss_mb}MB>=${RSS_LIMIT_MB}MB"
                sleep 5
            fi
        fi
    fi
done
