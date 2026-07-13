#!/bin/zsh
set -euo pipefail

ROOT="/Users/levies/Documents/twscape"
PID_FILE="$ROOT/monitor_panel_runtime/panel.pid"
LOG_FILE="$ROOT/monitor_panel_runtime/panel.log"
SERVER_INFO="$ROOT/monitor_panel_runtime/panel_server.json"

if [[ -f "$SERVER_INFO" ]]; then
  PANEL_URL="$(python3 - <<'PY'
import json
from pathlib import Path
path = Path("/Users/levies/Documents/twscape/monitor_panel_runtime/panel_server.json")
try:
    data = json.loads(path.read_text(encoding="utf-8"))
    print((data.get("server_url") or "").rstrip("/") + "/api/status")
except Exception:
    print("http://127.0.0.1:8765/api/status")
PY
)"
else
  PANEL_URL="http://127.0.0.1:8765/api/status"
fi

if [[ -f "$PID_FILE" ]]; then
  PANEL_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
else
  PANEL_PID=""
fi

if [[ -n "${PANEL_PID}" ]] && kill -0 "$PANEL_PID" 2>/dev/null; then
  echo "面板进程运行中，PID: $PANEL_PID"
else
  echo "面板进程未运行。"
fi

if curl -sS "$PANEL_URL" >/dev/null 2>&1; then
  echo "网页接口正常：$PANEL_URL"
else
  echo "网页接口不可访问：$PANEL_URL"
fi

if [[ -f "$LOG_FILE" ]]; then
  echo ""
  echo "最近日志："
  tail -n 20 "$LOG_FILE"
fi
