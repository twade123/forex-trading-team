#!/usr/bin/env bash
#
# trading_launcher.sh — Start/stop the trading system
#
# Usage:
#   ./trading_launcher.sh start    # Start dashboard + scout
#   ./trading_launcher.sh stop     # Graceful shutdown
#   ./trading_launcher.sh status   # Check if running
#   ./trading_launcher.sh restart  # Stop then start
#
# Port-aware: detects running services by port (lsof) regardless of PID files.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_DIR="$SCRIPT_DIR/.pids"
LOG_DIR="$SCRIPT_DIR/logs"
PYTHON="~/myenv/bin/python"

CAFFEINATE_PID="$PID_DIR/caffeinate.pid"

DASHBOARD_NAME="dashboard"
DASHBOARD_CMD="$PYTHON ~/jarvis/serve_ui.py"
DASHBOARD_PID="$PID_DIR/dashboard.pid"
DASHBOARD_LOG="$LOG_DIR/dashboard.log"
DASHBOARD_PORT=8766

SCOUT_NAME="scout"
SCOUT_CMD="$PYTHON -m trade_scout"
SCOUT_PID="$PID_DIR/scout.pid"
SCOUT_LOG="$LOG_DIR/scout.log"
SCOUT_PORT=8767

MLX_TA_NAME="mlx-35b"
MLX_TA_CMD="$PYTHON ~/jarvis/scripts/mlx_vlm_server_with_tools.py --model mlx-community/Qwen3.5-35B-A3B-4bit --adapter-path ~/Jarvis/models/adapters/35b_mlx --port 11502 --host 127.0.0.1"
MLX_TA_PID="$PID_DIR/mlx_35b.pid"
MLX_TA_LOG="$LOG_DIR/mlx_35b.log"
MLX_TA_PORT=11502

# Serving gateway — multi-tenant priority queue + pinned-prompt warmer in front of MLX 35B.
GATEWAY_NAME="serving-gateway"
GATEWAY_CMD="$PYTHON -m serving.run_gateway"
GATEWAY_PID="$PID_DIR/serving_gateway.pid"
GATEWAY_LOG="$LOG_DIR/serving_gateway.log"
GATEWAY_PORT=11503

WATCHDOG_NAME="watchdog"
WATCHDOG_CMD="$PYTHON ~/jarvis/trading_watchdog.py"
WATCHDOG_PID="$PID_DIR/watchdog.pid"
WATCHDOG_LOG="~/jarvis/logs/watchdog.log"

mkdir -p "$PID_DIR" "$LOG_DIR"

# --- Helpers ---

# Get PID listening on a port (empty if none)
pid_on_port() {
    lsof -iTCP:"$1" -sTCP:LISTEN -t 2>/dev/null | head -1 || true
}

# Check if a service is running — port check first, PID file fallback
is_running() {
    local pidfile="$1" port="$2"
    
    # Primary: check port
    local port_pid
    port_pid=$(pid_on_port "$port")
    if [[ -n "$port_pid" ]]; then
        # Update PID file to match reality
        echo "$port_pid" > "$pidfile"
        return 0
    fi
    
    # Fallback: check PID file
    if [[ -f "$pidfile" ]]; then
        local pid
        pid=$(cat "$pidfile")
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        rm -f "$pidfile"
    fi
    return 1
}

# Get the actual PID (port-aware)
get_pid() {
    local pidfile="$1" port="$2"
    local port_pid
    port_pid=$(pid_on_port "$port")
    if [[ -n "$port_pid" ]]; then
        echo "$port_pid"
        return
    fi
    if [[ -f "$pidfile" ]]; then
        cat "$pidfile"
    fi
}

WATCHDOG_PLIST="$HOME/Library/LaunchAgents/com.jarvis.trading_watchdog.plist"

start_watchdog() {
    # Prefer launchd-managed watchdog (survives crashes in the trading process group)
    if [[ -f "$WATCHDOG_PLIST" ]]; then
        # Kill any existing nohup-launched watchdog first
        local existing
        existing=$(pgrep -f "trading_watchdog.py" | head -1 || true)
        if [[ -n "$existing" ]]; then
            kill "$existing" 2>/dev/null || true
            sleep 1
        fi
        # Load via launchd so it runs in its own process group
        launchctl unload "$WATCHDOG_PLIST" 2>/dev/null || true
        launchctl load "$WATCHDOG_PLIST"
        sleep 1
        local pid
        pid=$(pgrep -f "trading_watchdog.py" | head -1 || true)
        if [[ -n "$pid" ]]; then
            echo "[$WATCHDOG_NAME] Started via launchd (PID $pid) — crash-proof"
        else
            echo "[$WATCHDOG_NAME] Started via launchd (loading...)"
        fi
        return 0
    fi

    # Fallback: nohup launch (old behavior)
    if pgrep -f "trading_watchdog.py" > /dev/null 2>&1; then
        local pid
        pid=$(pgrep -f "trading_watchdog.py" | head -1)
        echo "[$WATCHDOG_NAME] Already running (PID $pid)"
        return 0
    fi
    echo "[$WATCHDOG_NAME] Starting (nohup fallback)..."
    nohup bash -c "$WATCHDOG_CMD" >> "$WATCHDOG_LOG" 2>&1 &
    local pid=$!
    echo "$pid" > "$WATCHDOG_PID"
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
        echo "[$WATCHDOG_NAME] Started (PID $pid)"
    else
        echo "[$WATCHDOG_NAME] FAILED to start — check $WATCHDOG_LOG"
    fi
}

stop_watchdog() {
    # Unload from launchd if managed that way
    if [[ -f "$WATCHDOG_PLIST" ]]; then
        launchctl unload "$WATCHDOG_PLIST" 2>/dev/null || true
    fi
    local pid
    pid=$(pgrep -f "trading_watchdog.py" | head -1 || true)
    if [[ -z "$pid" ]] && [[ -f "$WATCHDOG_PID" ]]; then
        pid=$(cat "$WATCHDOG_PID")
    fi
    if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
        echo "[$WATCHDOG_NAME] Not running"
        rm -f "$WATCHDOG_PID"
        return 0
    fi
    echo "[$WATCHDOG_NAME] Stopping (PID $pid)..."
    kill "$pid" 2>/dev/null || true
    local waited=0
    while kill -0 "$pid" 2>/dev/null && [[ $waited -lt 10 ]]; do
        sleep 1; waited=$((waited + 1))
    done
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$WATCHDOG_PID"
    echo "[$WATCHDOG_NAME] Stopped"
}

start_service() {
    local name="$1" cmd="$2" pidfile="$3" logfile="$4" port="$5"

    if is_running "$pidfile" "$port"; then
        echo "[$name] Already running (PID $(get_pid "$pidfile" "$port")) on port $port"
        return 0
    fi

    echo "[$name] Starting..."

    # Rotate log if > 10MB
    if [[ -f "$logfile" ]] && [[ $(stat -f%z "$logfile" 2>/dev/null || stat -c%s "$logfile" 2>/dev/null) -gt 10485760 ]]; then
        mv "$logfile" "${logfile}.prev"
    fi

    # Launch in background
    cd "$SCRIPT_DIR"
    nohup bash -c "$cmd" >> "$logfile" 2>&1 &
    local pid=$!
    echo "$pid" > "$pidfile"
    
    # Wait up to 10s for port to open
    local waited=0
    while [[ $waited -lt 10 ]]; do
        sleep 1
        waited=$((waited + 1))
        if [[ -n "$(pid_on_port "$port")" ]]; then
            echo "[$name] Started (PID $pid) on port $port"
            return 0
        fi
    done

    # Check if process is at least alive (some services take longer)
    if kill -0 "$pid" 2>/dev/null; then
        echo "[$name] Started (PID $pid) — port $port not yet open (may still be initializing)"
    else
        echo "[$name] FAILED to start — check $logfile"
        rm -f "$pidfile"
        return 1
    fi
}

stop_service() {
    local name="$1" pidfile="$2" port="$3"

    # Snapshot all matching PIDs NOW — before killing anything.
    # _kill_orphans will use these instead of re-scanning (which would hit newly started processes).
    local pattern=""
    case "$name" in
        scout)       pattern="trade_scout" ;;
        dashboard)   pattern="serve_ui.py" ;;
        mlx-35b)     pattern="mlx_vlm_server_with_tools.*11502" ;;
    esac
    if [[ -n "$pattern" ]]; then
        local pids_var="_ORPHAN_PIDS_${name//-/_}"
        eval "$pids_var"'=$(pgrep -f "$pattern" 2>/dev/null || true)'
    fi

    if ! is_running "$pidfile" "$port"; then
        echo "[$name] Not running"
        # Still sweep for orphans (process alive but lost its port)
        _kill_orphans "$name"
        rm -f "$pidfile"
        return 0
    fi

    local pid
    pid=$(get_pid "$pidfile" "$port")
    echo "[$name] Stopping (PID $pid)..."

    # Graceful SIGTERM, wait up to 15s
    kill "$pid" 2>/dev/null || true
    local waited=0
    while kill -0 "$pid" 2>/dev/null && [[ $waited -lt 15 ]]; do
        sleep 1
        waited=$((waited + 1))
    done

    # Force kill if still alive
    if kill -0 "$pid" 2>/dev/null; then
        echo "[$name] Force killing..."
        kill -9 "$pid" 2>/dev/null || true
        sleep 1
    fi

    # Double-check port is freed
    local remaining
    remaining=$(pid_on_port "$port")
    if [[ -n "$remaining" ]]; then
        echo "[$name] Port $port still held by PID $remaining — killing..."
        kill -9 "$remaining" 2>/dev/null || true
        sleep 1
    fi

    # Kill any orphan processes (alive but lost their port binding)
    _kill_orphans "$name"

    rm -f "$pidfile"
    echo "[$name] Stopped"
}

# Kill orphan processes by matching their command pattern.
# Catches processes that lost their port (e.g. old scout still alive after new one took 8767).
# Only kills PIDs captured BEFORE stop_service runs — never kills newly started processes.
_kill_orphans() {
    local name="$1"
    local pattern=""
    case "$name" in
        scout)       pattern="trade_scout" ;;
        dashboard)   pattern="serve_ui.py" ;;
        mlx-35b)     pattern="mlx_vlm_server_with_tools.*11502" ;;
        *)           return 0 ;;
    esac

    # Use pre-captured PIDs if available (set by stop_service before killing)
    local pids_var="_ORPHAN_PIDS_${name//-/_}"
    local orphans="${!pids_var}"
    if [[ -z "$orphans" ]]; then
        orphans=$(pgrep -f "$pattern" 2>/dev/null || true)
    fi
    if [[ -n "$orphans" ]]; then
        local count
        count=$(echo "$orphans" | wc -l | tr -d ' ')
        echo "[$name] Killing $count orphan process(es)..."
        echo "$orphans" | xargs kill -9 2>/dev/null || true
        sleep 1
    fi
}

# --- Commands ---

do_start() {
    echo "=== Trading System Starting ==="
    echo "$(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo ""

    # MLX 35B agent fleet (Qwen3.5-35B — port 11502, validator + 7 swarm-dispatched trading agents + 4 direct-call helpers)
    start_service "$MLX_TA_NAME" "$MLX_TA_CMD" "$MLX_TA_PID" "$MLX_TA_LOG" "$MLX_TA_PORT"
    sleep 2

    # Pre-warm 35B SEQUENTIALLY — block until it responds before starting dashboard.
    # First cycle must not hit cold model or validator/TA/narrator will timeout.
    # Payload is a realistic prompt that forces processor init + non-trivial prefill
    # so the first real request (TA ~8K tokens, validator ~7K tokens) runs at steady-state speed.
    local _WARMUP_35B='{"model":"x","messages":[{"role":"system","content":"You are a forex trade validator. Form a thesis and return JSON verdict."},{"role":"user","content":"Warmup: EUR_USD M15 neutral. Fan flat, BBs contracting, RSI 50. Return JSON {\"verdict\":\"SKIP\",\"direction\":null,\"confidence\":1,\"reasoning\":\"warmup\"}."}],"max_tokens":64,"temperature":0,"chat_template_kwargs":{"enable_thinking":false}}'

    echo "[mlx] Warming 35B (blocking, realistic payload)..."
    for i in $(seq 1 30); do
        if curl -s --max-time 60 -X POST "http://127.0.0.1:$MLX_TA_PORT/v1/chat/completions" \
            -H "Content-Type: application/json" \
            -d "$_WARMUP_35B" \
            > /dev/null 2>&1; then
            echo "[mlx] 35B warm ✓"
            break
        fi
        sleep 3
    done

    # Serving gateway — multi-tenant priority queue + pinned-prompt warmer.
    # Starts after MLX is warm so the warmer has a live backend.
    echo "[gateway] starting on port $GATEWAY_PORT..."
    cd "$SCRIPT_DIR" && \
        nohup $PYTHON -m serving.run_gateway >> "$GATEWAY_LOG" 2>&1 &
    GW_PID=$!
    echo "$GW_PID" > "$GATEWAY_PID"
    for i in $(seq 1 30); do
        if curl -s --max-time 2 "http://127.0.0.1:$GATEWAY_PORT/healthz" > /dev/null 2>&1; then
            echo "[gateway] ready (PID $GW_PID, port $GATEWAY_PORT)"
            break
        fi
        sleep 1
    done

    start_service "$DASHBOARD_NAME" "$DASHBOARD_CMD" "$DASHBOARD_PID" "$DASHBOARD_LOG" "$DASHBOARD_PORT"
    sleep 3

    # Scout now runs INSIDE serve_ui as a background thread (2026-03-30).
    # No separate process — eliminates cross-process mmap corruption on trading_forex.db.
    # start_service "$SCOUT_NAME" "$SCOUT_CMD" "$SCOUT_PID" "$SCOUT_LOG" "$SCOUT_PORT"
    # sleep 3

    # Watchdog monitors serve_ui and auto-restarts if it goes down
    start_watchdog

    # Keep Mac awake
    if ! pgrep -x caffeinate > /dev/null 2>&1; then
        caffeinate -dims &
        echo "$!" > "$CAFFEINATE_PID"
        echo "[caffeinate] Mac will stay awake while trading is active"
    else
        echo "[caffeinate] Already running"
    fi

    echo ""
    echo "=== Trading System Running ==="
    echo "Dashboard:  http://localhost:$DASHBOARD_PORT/trading"
    echo "Scout WS:   ws://localhost:$SCOUT_PORT"
}

do_stop() {
    echo "=== Trading System Stopping ==="
    echo "$(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo ""

    stop_watchdog
    stop_service "$SCOUT_NAME" "$SCOUT_PID" "$SCOUT_PORT"
    stop_service "$DASHBOARD_NAME" "$DASHBOARD_PID" "$DASHBOARD_PORT"
    # Stop gateway BEFORE MLX so in-flight gateway requests don't error mid-shutdown.
    if [ -f "$GATEWAY_PID" ]; then
        gw_pid=$(cat "$GATEWAY_PID")
        if kill -0 "$gw_pid" 2>/dev/null; then
            echo "[gateway] stopping (PID $gw_pid)..."
            kill "$gw_pid" 2>/dev/null || true
            sleep 2
            kill -9 "$gw_pid" 2>/dev/null || true
        fi
        rm -f "$GATEWAY_PID"
    fi
    stop_service "$MLX_TA_NAME" "$MLX_TA_PID" "$MLX_TA_PORT"

    # Release caffeinate
    if [[ -f "$CAFFEINATE_PID" ]]; then
        kill "$(cat "$CAFFEINATE_PID")" 2>/dev/null || true
        rm -f "$CAFFEINATE_PID"
    fi
    # Kill any caffeinate we spawned
    pkill -x caffeinate 2>/dev/null || true
    echo "[caffeinate] Released — Mac can sleep now"

    echo ""
    echo "=== Trading System Stopped ==="
}

do_status() {
    echo "=== Trading System Status ==="
    for svc in "$MLX_TA_NAME:$MLX_TA_PID:$MLX_TA_PORT" "$GATEWAY_NAME:$GATEWAY_PID:$GATEWAY_PORT" "$DASHBOARD_NAME:$DASHBOARD_PID:$DASHBOARD_PORT" "$SCOUT_NAME:$SCOUT_PID:$SCOUT_PORT"; do
        IFS=: read -r name pidfile port <<< "$svc"
        if is_running "$pidfile" "$port"; then
            local pid
            pid=$(get_pid "$pidfile" "$port")
            echo "  $name: RUNNING (PID $pid) on port $port"
        else
            echo "  $name: STOPPED (port $port free)"
        fi
    done
    
    local wpid
    wpid=$(pgrep -f "trading_watchdog.py" | head -1 || true)
    if [[ -n "$wpid" ]]; then
        echo "  $WATCHDOG_NAME: RUNNING (PID $wpid)"
    else
        echo "  $WATCHDOG_NAME: STOPPED"
    fi

    if pgrep -x caffeinate > /dev/null 2>&1; then
        echo "  caffeinate: ACTIVE"
    else
        echo "  caffeinate: OFF"
    fi
}

do_restart() {
    # ── CRITICAL: Pause watchdog BEFORE stopping services ──
    # Without this, the watchdog detects services going down during stop
    # and tries to restart them — racing with do_start() and creating
    # duplicate processes or killing freshly launched ones.
    echo "[restart] Pausing watchdog for 120s to prevent race condition..."
    local pause_file="~/jarvis/watchdog.pause"
    local expires=$(($(date +%s) + 120))
    echo "{\"expires\": $expires, \"reason\": \"launcher_restart\"}" > "$pause_file"

    do_stop
    sleep 3

    # Extra: sweep for any scout processes the watchdog may have spawned
    # between our stop and the pause taking effect
    local stray_scouts
    stray_scouts=$(pgrep -f "trade_scout" 2>/dev/null || true)
    if [[ -n "$stray_scouts" ]]; then
        echo "[restart] Killing stray scout(s) from watchdog race: $stray_scouts"
        echo "$stray_scouts" | xargs kill -9 2>/dev/null || true
        sleep 1
    fi

    do_start

    # Remove pause file — watchdog resumes after its next check
    rm -f "$pause_file"
    echo "[restart] Watchdog pause released"
}

# Code-only reload: restart serve_ui + watchdog, leave MLX 35B alone.
# Use for Python code changes that don't touch MLX server scripts. Model stays
# warm across the reload — first validator/TA call runs at full speed.
# Use `restart` (full) only when you change mlx_vlm_server_with_tools.py or swap adapters.
do_reload() {
    echo "=== Trading System Code Reload (MLX stays up) ==="
    echo "$(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo ""

    # Verify MLX servers are actually up — if not, suggest full restart.
    # 2026-04-24: switched from curl HTTP probe to process + port probe
    # because MLX servers legitimately refuse HTTP while mid-inference
    # (they're single-threaded at the inference layer). Prior curl with
    # 3s timeout falsely reported "NOT responding" for busy servers,
    # blocking code reloads when models were doing their job.
    _mlx_alive() {
        local port="$1"
        local pidfile="$2"
        # Process alive from PID file?
        if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null; then
            return 0
        fi
        # Fallback: something listening on the port?
        if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
            return 0
        fi
        # Last resort: pgrep the server pattern with port match
        if pgrep -f "mlx_vlm_server_with_tools.*$port" >/dev/null 2>&1; then
            return 0
        fi
        return 1
    }

    if ! _mlx_alive "$MLX_TA_PORT" "$MLX_TA_PID"; then
        echo "[reload] ⚠️  35B (port $MLX_TA_PORT) is NOT running — run 'restart' instead of 'reload'"
        exit 1
    fi
    echo "[reload] MLX 35B alive (process + port check) — leaving it up"

    # Pause watchdog so it doesn't restart serve_ui mid-reload
    echo "[reload] Pausing watchdog for 60s..."
    local pause_file="~/jarvis/watchdog.pause"
    local expires=$(($(date +%s) + 60))
    echo "{\"expires\": $expires, \"reason\": \"launcher_reload\"}" > "$pause_file"

    # Stop ONLY serve_ui + watchdog — MLX processes untouched
    stop_watchdog
    stop_service "$DASHBOARD_NAME" "$DASHBOARD_PID" "$DASHBOARD_PORT"
    sleep 2

    # Start serve_ui + watchdog back up
    start_service "$DASHBOARD_NAME" "$DASHBOARD_CMD" "$DASHBOARD_PID" "$DASHBOARD_LOG" "$DASHBOARD_PORT"
    sleep 3
    start_watchdog

    # Release pause
    rm -f "$pause_file"
    echo "[reload] Watchdog pause released"
    echo ""
    echo "=== Code Reload Complete — MLX models stayed warm ==="
}

# --- Main ---

case "${1:-}" in
    start)   do_start ;;
    stop)    do_stop ;;
    status)  do_status ;;
    restart) do_restart ;;
    reload)  do_reload ;;
    *)
        echo "Usage: $0 {start|stop|status|restart|reload}"
        echo ""
        echo "  restart  Full restart incl. MLX 35B (60s cold warmup)"
        echo "  reload   Code-only restart — leaves MLX 35B warm (Python changes)"
        exit 1
        ;;
esac
