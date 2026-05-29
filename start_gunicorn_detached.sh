#!/usr/bin/env bash
# Launch the supervised gunicorn fully detached so it survives the death of
# whatever shell or harness launched it. Uses `setsid` to put the process in
# its own session (so SIGHUP/SIGTERM from a closing parent shell can't
# cascade) and `nohup` for belt-and-braces.
#
# Logs go to gunicorn.log (in the project root) instead of the parent shell's
# stdout, since stdout closes when the parent dies.
#
# Usage:   ./start_gunicorn_detached.sh
# Stop:    pkill -SIGINT -f "bash.*run_gunicorn.sh"

cd "$(dirname "$0")"

# If anything's already serving on 8765, refuse to double-launch.
if lsof -ti :8765 >/dev/null 2>&1; then
    echo "Something is already listening on :8765 — not starting another."
    exit 0
fi

LOG="$(pwd)/gunicorn.log"
echo "[detached-launcher] $(date)  starting supervised gunicorn, logs → $LOG" >> "$LOG"

# macOS doesn't ship `setsid`. nohup + disown + the supervisor's own
# `trap '' TERM HUP` inside run_gunicorn.sh is enough:
#   nohup    → child ignores SIGHUP from parent shell closing.
#   </dev/null → no stdin to break on parent EOF.
#   & disown → detach from this shell's job table.
nohup bash ./run_gunicorn.sh </dev/null >> "$LOG" 2>&1 &
disown

# Tiny pause + readiness check
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    sleep 1
    code=$(curl -s -o /dev/null --max-time 1 -w "%{http_code}" "http://127.0.0.1:8765/" 2>/dev/null || echo 000)
    case "$code" in
        200|301|302|401|403)
            echo "Server is up (HTTP $code) after ${i}s. Tail logs with: tail -f $LOG"
            # Start the watchdog too (skip silently if already running — its
            # PID-file check refuses to double-launch).
            WATCHLOG="$(pwd)/watchdog.log"
            nohup bash ./watchdog.sh </dev/null >> "$WATCHLOG" 2>&1 &
            disown
            echo "Watchdog started; logs → $WATCHLOG"
            exit 0
            ;;
    esac
done
echo "Server didn't respond within 12s — check $LOG"
exit 1
