#!/bin/zsh
set -euo pipefail

ROOT="/Users/levies/Documents/twscape"
RUNTIME_DIR="$ROOT/monitor_panel_runtime"
PID_FILE="$RUNTIME_DIR/panel.pid"
LOG_FILE="$RUNTIME_DIR/panel.log"
URL="http://127.0.0.1:8765/"
STATUS_URL="http://127.0.0.1:8765/api/status"

mkdir -p "$RUNTIME_DIR"

if [[ -f "$PID_FILE" ]]; then
  EXISTING_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [[ -n "${EXISTING_PID}" ]] && kill -0 "$EXISTING_PID" 2>/dev/null; then
    echo "面板已经在后台运行中：$URL"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

cd "$ROOT"
source "$ROOT/twscrape-main/.venv/bin/activate"

nohup python -u "$ROOT/monitor_panel.py" --no-browser > "$LOG_FILE" 2>&1 &
PANEL_PID=$!
echo "$PANEL_PID" > "$PID_FILE"

for _ in {1..10}; do
  if curl -sS "$STATUS_URL" >/dev/null 2>&1; then
    echo "面板已在后台启动：$URL"
    echo "日志文件：$LOG_FILE"
    open "$URL" >/dev/null 2>&1 || true
    exit 0
  fi

  if ! kill -0 "$PANEL_PID" 2>/dev/null; then
    echo "面板启动后立即退出了，请检查日志：$LOG_FILE"
    tail -n 30 "$LOG_FILE" || true
    rm -f "$PID_FILE"
    exit 1
  fi

  sleep 1
done

echo "面板进程还在，但网页接口没有成功打开。"
echo "请检查日志：$LOG_FILE"
tail -n 30 "$LOG_FILE" || true
exit 1
