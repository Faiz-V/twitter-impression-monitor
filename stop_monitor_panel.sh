#!/bin/zsh
set -euo pipefail

ROOT="/Users/levies/Documents/twscape"
PID_FILE="$ROOT/monitor_panel_runtime/panel.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "没有找到面板进程记录。"
  exit 0
fi

PANEL_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -z "${PANEL_PID}" ]]; then
  rm -f "$PID_FILE"
  echo "面板进程记录为空，已清理。"
  exit 0
fi

if kill -0 "$PANEL_PID" 2>/dev/null; then
  kill "$PANEL_PID" 2>/dev/null || true
  sleep 1
fi

rm -f "$PID_FILE"
echo "面板已停止。"
