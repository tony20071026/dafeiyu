#!/usr/bin/env bash
# 大肥鱼守护脚本：cron @reboot 调用 start，崩溃后自动重启。
set -u

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$BASE_DIR/.venv/bin/python"
BOT="$BASE_DIR/bot.py"
PID_FILE="$BASE_DIR/data/dafeiyu.pid"
STOP_FILE="$BASE_DIR/data/dafeiyu.stop"
SERVICE_LOG="$BASE_DIR/logs/service.log"
RESTART_DELAY=10

mkdir -p "$BASE_DIR/data" "$BASE_DIR/logs"

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

is_running() {
    [ -f "$PID_FILE" ] || return 1
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    case "$pid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    kill -0 "$pid" 2>/dev/null
}

supervise() {
    rm -f "$STOP_FILE"
    local child=""

    trap 'log "守护进程收到停止信号"; if [ -n "$child" ]; then kill -TERM "$child" 2>/dev/null; fi; exit 0' TERM INT

    while :; do
        log "启动大肥鱼…"
        cd "$BASE_DIR" || exit 1
        PYTHONDONTWRITEBYTECODE=1 "$PYTHON" "$BOT" &
        child=$!
        wait "$child"
        local code=$?
        child=""
        log "大肥鱼退出，退出码=$code"

        if [ -f "$STOP_FILE" ]; then
            rm -f "$STOP_FILE"
            log "收到停止标记，守护进程结束"
            exit 0
        fi

        log "${RESTART_DELAY} 秒后自动重启"
        sleep "$RESTART_DELAY"
    done
}

start() {
    if is_running; then
        log "大肥鱼已在运行，pid=$(cat "$PID_FILE")"
        return 0
    fi
    rm -f "$PID_FILE" "$STOP_FILE"

    set -m
    nohup "$0" _supervise >>"$SERVICE_LOG" 2>&1 &
    local pid=$!
    set +m
    echo "$pid" > "$PID_FILE"

    sleep 1
    if is_running; then
        log "大肥鱼已启动，pid=$pid"
        return 0
    fi
    log "启动失败，请查看 $SERVICE_LOG"
    return 1
}

stop() {
    if ! is_running; then
        rm -f "$PID_FILE" "$STOP_FILE"
        log "大肥鱼未运行"
        return 0
    fi

    local pid
    pid="$(cat "$PID_FILE")"
    touch "$STOP_FILE"
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true

    local i
    for i in $(seq 1 20); do
        is_running || break
        sleep 0.5
    done

    if is_running; then
        kill -KILL -- "-$pid" 2>/dev/null || true
        sleep 0.5
    fi

    rm -f "$PID_FILE" "$STOP_FILE"
    log "大肥鱼已停止"
}

status() {
    if is_running; then
        log "大肥鱼运行中，pid=$(cat "$PID_FILE")"
        return 0
    fi
    log "大肥鱼未运行"
    return 1
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    restart) stop; start ;;
    status) status ;;
    _supervise) supervise ;;
    *)
        echo "用法: $0 {start|stop|restart|status}"
        exit 2
        ;;
esac