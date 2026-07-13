#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from twscrape import AccountsPool


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / ".monitor_panel_config.json"
RUNTIME_DIR = ROOT / "monitor_panel_runtime"
RUNTIME_DIR.mkdir(exist_ok=True)
TASKS_PATH = RUNTIME_DIR / "tasks.json"
SERVER_INFO_PATH = RUNTIME_DIR / "panel_server.json"
SCRIPT_PATH = ROOT / "twscrape-main" / "scripts" / "monitor_impressions.py"
DEFAULT_DB = "accounts.db"

APP_STATE = {
    "server_url": None,
}


def now_local() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def default_config() -> dict:
    return {
        "username": "",
        "auth_token": "",
        "ct0": "",
        "tweet_input": "",
        "db_path": DEFAULT_DB,
        "selected_tweet_id": "",
    }


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return default_config()
    try:
        return {**default_config(), **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))}
    except json.JSONDecodeError:
        return default_config()


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def load_tasks() -> dict[str, dict]:
    if not TASKS_PATH.exists():
        return {}
    try:
        data = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    return {}


def save_tasks(tasks: dict[str, dict]) -> None:
    TASKS_PATH.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")


def save_server_info(server_url: str, port: int) -> None:
    payload = {
        "server_url": server_url,
        "port": port,
        "updated_at": now_local(),
    }
    SERVER_INFO_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_tweet_id(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value.isdigit():
        return value
    marker = "/status/"
    if marker in value:
        tail = value.split(marker, 1)[1]
        digits = []
        for ch in tail:
            if ch.isdigit():
                digits.append(ch)
            else:
                break
        return "".join(digits)
    return ""


def task_state_path(tweet_id: str) -> Path:
    return ROOT / "impression_logs" / f"tweet_{tweet_id}_state.json"


def task_report_path(tweet_id: str) -> Path:
    return ROOT / "impression_logs" / f"tweet_{tweet_id}_report.html"


def task_csv_path(tweet_id: str) -> Path:
    return ROOT / "impression_logs" / f"tweet_{tweet_id}_impressions.csv"


def task_jsonl_path(tweet_id: str) -> Path:
    return ROOT / "impression_logs" / f"tweet_{tweet_id}_impressions.jsonl"


def read_task_state(tweet_id: str) -> dict | None:
    path = task_state_path(tweet_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def latest_report_path() -> Path | None:
    out_dir = ROOT / "impression_logs"
    if not out_dir.exists():
        return None
    reports = sorted(out_dir.glob("tweet_*_report.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    return reports[0] if reports else None


def is_pid_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def build_task_summary(task: dict) -> dict:
    tweet_id = str(task.get("tweet_id", ""))
    state = read_task_state(tweet_id) if tweet_id else None
    pid = task.get("pid")
    running = is_pid_running(pid)
    report_path = task_report_path(tweet_id)
    csv_path = task_csv_path(tweet_id)

    status = "running" if running else task.get("status", "idle")
    if state and state.get("status"):
        status = state["status"]
    elif not running and status == "running":
        status = "stopped"

    latest = (state or {}).get("latest") or {}
    summary = {
        **task,
        "tweet_id": tweet_id,
        "pid_running": running,
        "status": status,
        "state": state,
        "latest": latest,
        "report_available": report_path.exists() and report_path.is_file(),
        "csv_available": csv_path.exists() and csv_path.is_file(),
    }
    return summary


def all_task_summaries() -> list[dict]:
    tasks = load_tasks()
    items = [build_task_summary(task) for task in tasks.values()]
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return items


def resolve_selected_tweet_id(query: dict | None = None) -> str:
    query = query or {}
    requested = (query.get("tweet_id", [""])[0] or "").strip()
    if requested:
        return requested

    config = load_config()
    selected = config.get("selected_tweet_id", "")
    if selected:
        return selected

    tasks = all_task_summaries()
    if tasks:
        return str(tasks[0].get("tweet_id", ""))
    return ""


async def refresh_account_cookie(db_path: str, username: str, auth_token: str, ct0: str) -> None:
    pool = AccountsPool(db_path)
    cookies = f"auth_token={auth_token}; ct0={ct0}"
    existing = await pool.get_account(username)
    if existing is not None:
        await pool.delete_accounts(username)
    await pool.add_account_cookies(username, cookies)


def start_monitor(payload: dict) -> dict:
    username = (payload.get("username") or "").strip()
    auth_token = (payload.get("auth_token") or "").strip()
    ct0 = (payload.get("ct0") or "").strip()
    tweet_input = (payload.get("tweet_input") or "").strip()
    db_path = (payload.get("db_path") or DEFAULT_DB).strip() or DEFAULT_DB

    if not username or not auth_token or not ct0 or not tweet_input:
        raise ValueError("用户名、auth_token、ct0、推文链接/ID 都要填写。")

    tweet_id = parse_tweet_id(tweet_input) or tweet_input
    if not tweet_id:
        raise ValueError("无法识别推文链接或推文 ID。")

    tasks = load_tasks()
    existing = tasks.get(tweet_id)
    if existing and is_pid_running(existing.get("pid")):
        raise RuntimeError(f"推文 {tweet_id} 已经在监测中了。")

    asyncio.run(refresh_account_cookie(db_path, username, auth_token, ct0))

    config = load_config()
    config.update(
        {
            "username": username,
            "auth_token": auth_token,
            "ct0": ct0,
            "tweet_input": tweet_input,
            "db_path": db_path,
            "selected_tweet_id": tweet_id,
        }
    )
    save_config(config)

    log_path = RUNTIME_DIR / f"tweet_{tweet_id}.log"
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [sys.executable, str(SCRIPT_PATH), "--db", db_path, "--tweet", tweet_input]
    caffeinate = shutil.which("caffeinate")
    if caffeinate:
        cmd = [caffeinate, "-dimsu", *cmd]

    with log_path.open("a", encoding="utf-8") as log_fp:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=log_fp,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )

    tasks[tweet_id] = {
        "tweet_id": tweet_id,
        "tweet_input": tweet_input,
        "username": username,
        "db_path": db_path,
        "pid": proc.pid,
        "log_path": str(log_path),
        "created_at": existing.get("created_at") if existing else now_local(),
        "last_started_at": now_local(),
        "status": "running",
    }
    save_tasks(tasks)

    return {"ok": True, "tweet_id": tweet_id, "message": f"推文 {tweet_id} 的监测已启动"}


def stop_monitor(tweet_id: str) -> dict:
    tweet_id = (tweet_id or "").strip()
    if not tweet_id:
        raise ValueError("停止任务时需要 tweet_id。")

    tasks = load_tasks()
    task = tasks.get(tweet_id)
    if not task:
        return {"ok": True, "message": f"没有找到推文 {tweet_id} 的任务。"}

    pid = task.get("pid")
    if is_pid_running(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    task["status"] = "stopped"
    task["stopped_at"] = now_local()
    tasks[tweet_id] = task
    save_tasks(tasks)
    return {"ok": True, "message": f"推文 {tweet_id} 的监测已停止"}


def status_payload(query: dict | None = None) -> dict:
    query = query or {}
    config = load_config()
    tasks = all_task_summaries()
    selected_tweet_id = resolve_selected_tweet_id(query)
    selected_task = next((x for x in tasks if str(x.get("tweet_id")) == selected_tweet_id), None)
    report_path = task_report_path(selected_tweet_id) if selected_tweet_id else latest_report_path()
    report_available = bool(report_path and report_path.exists() and report_path.is_file())

    if selected_tweet_id:
        config["selected_tweet_id"] = selected_tweet_id
        save_config(config)

    return {
        "tasks": tasks,
        "selected_tweet_id": selected_tweet_id,
        "selected_task": selected_task,
        "config": config,
        "report_available": report_available,
        "server_url": APP_STATE.get("server_url"),
    }


def html_page() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Twitter Impression Monitor</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f7fb;
      --panel: #ffffff;
      --line: #dbe3ee;
      --text: #0f172a;
      --muted: #475569;
      --accent: #1d4ed8;
      --accent-2: #0f766e;
      --warn: #b45309;
      --soft: #f8fbff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background:
        radial-gradient(circle at top right, rgba(37,99,235,0.08), transparent 28%),
        linear-gradient(180deg, #eff5ff, var(--bg) 280px);
    }
    .wrap {
      max-width: 1380px;
      margin: 0 auto;
      padding: 28px 18px 36px;
      display: grid;
      gap: 18px;
    }
    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: flex-end;
      gap: 16px;
      flex-wrap: wrap;
    }
    h1 { margin: 0; font-size: 30px; line-height: 1.1; }
    .sub { color: var(--muted); font-size: 14px; margin-top: 8px; }
    .footer-meta {
      display: flex;
      gap: 16px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 13px;
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(320px, 420px) minmax(360px, 460px) minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 12px 28px rgba(15, 23, 42, 0.05);
      overflow: hidden;
    }
    .panel-body { padding: 18px; }
    .panel-title { font-size: 18px; font-weight: 700; margin: 0 0 6px; }
    .panel-copy { color: var(--muted); font-size: 13px; line-height: 1.5; margin: 0 0 14px; }
    label { display: block; margin-bottom: 14px; }
    .label { font-size: 13px; color: var(--muted); margin-bottom: 6px; }
    input, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      font-size: 14px;
      background: #fbfdff;
      color: var(--text);
    }
    textarea { min-height: 92px; resize: vertical; }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 8px; }
    button, .link-btn {
      border: 0;
      border-radius: 8px;
      padding: 10px 14px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 108px;
    }
    .primary { background: var(--accent); color: white; }
    .secondary { background: #e2e8f0; color: var(--text); }
    .ghost { background: #ecfeff; color: var(--accent-2); }
    .note { font-size: 13px; color: var(--warn); margin-top: 12px; line-height: 1.45; }
    .task-list { display: grid; gap: 10px; }
    .task-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--soft);
      padding: 14px;
      cursor: pointer;
    }
    .task-card.active { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
    .task-head {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: center;
      margin-bottom: 8px;
    }
    .task-id { font-weight: 700; font-size: 14px; word-break: break-all; }
    .badge {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 12px;
      font-weight: 700;
      background: #dbeafe;
      color: var(--accent);
      white-space: nowrap;
    }
    .badge.running { background: #dcfce7; color: #15803d; }
    .badge.completed { background: #e0f2fe; color: #0369a1; }
    .badge.stopped, .badge.error { background: #fee2e2; color: #b91c1c; }
    .task-meta { color: var(--muted); font-size: 13px; line-height: 1.5; }
    .metrics {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }
    .metric {
      background: var(--soft);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }
    .metric .m-label { color: var(--muted); font-size: 12px; margin-bottom: 8px; }
    .metric .m-value { font-size: 24px; font-weight: 700; }
    .status-box {
      background: var(--soft);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      margin-bottom: 14px;
      font-size: 14px;
      line-height: 1.5;
    }
    .task-actions { display: flex; gap: 10px; flex-wrap: wrap; }
    iframe {
      width: 100%;
      height: 980px;
      border: 0;
      background: white;
    }
    @media (max-width: 1180px) {
      .layout { grid-template-columns: 1fr; }
      iframe { height: 760px; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div>
        <h1>Twitter Impression Monitor</h1>
        <div class="sub">现在是一套多任务监测台。一天发三条推文，就开三条任务，各跑各的 72 小时，互不覆盖。</div>
      </div>
      <div class="footer-meta">
        <div>本地模式：合盖后仍会停止</div>
        <div>建议：运行期间保持电源连接</div>
      </div>
    </div>

    <div class="layout">
      <section class="panel">
        <div class="panel-body">
          <div class="panel-title">新建监测任务</div>
          <div class="panel-copy">填一次账号信息，后面每条推文都可以单独启动，面板会自动更新 cookie 并开启新的后台进程。</div>
          <label>
            <div class="label">账号用户名</div>
            <input id="username" placeholder="例如 Predx" />
          </label>
          <label>
            <div class="label">推文链接或推文 ID</div>
            <input id="tweet_input" placeholder="支持完整链接，也支持纯数字 ID" />
          </label>
          <label>
            <div class="label">auth_token</div>
            <textarea id="auth_token" placeholder="粘贴 auth_token"></textarea>
          </label>
          <label>
            <div class="label">ct0</div>
            <textarea id="ct0" placeholder="粘贴 ct0"></textarea>
          </label>
          <label>
            <div class="label">账号数据库路径</div>
            <input id="db_path" placeholder="accounts.db" />
          </label>
          <div class="actions">
            <button class="primary" id="start_btn">新增任务</button>
            <a class="link-btn ghost" id="open_report_btn" href="/report/current" target="_blank">打开当前报告</a>
          </div>
          <div class="note">如果本机进入睡眠或你合上电脑，所有本地任务都会暂停。面板会尽量用保活方式防止普通睡眠。</div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-body">
          <div class="panel-title">任务列表</div>
          <div class="panel-copy">每条推文是一个独立任务。点一条，就会在右侧切换到它的状态和报告。</div>
          <div id="task_list" class="task-list"></div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-body">
          <div class="panel-title">当前选中任务</div>
          <div class="panel-copy" id="selected_copy">选中一条任务后，这里会显示它的运行状态、关键数字和报告入口。</div>
          <div class="metrics">
            <div class="metric"><div class="m-label">状态</div><div class="m-value" id="metric_status">待命</div></div>
            <div class="metric"><div class="m-label">当前 Impression</div><div class="m-value" id="metric_views">-</div></div>
            <div class="metric"><div class="m-label">最近一次增量</div><div class="m-value" id="metric_delta">-</div></div>
            <div class="metric"><div class="m-label">采样次数</div><div class="m-value" id="metric_samples">0</div></div>
          </div>
          <div class="status-box" id="status_box">
            <div><span class="badge">等待输入</span></div>
            <div style="margin-top:8px;">右侧会显示所选任务的摘要和报告。</div>
          </div>
          <div class="task-actions">
            <button class="secondary" id="stop_btn">停止当前任务</button>
          </div>
        </div>
        <iframe id="report_frame" src="/report/current" title="monitor report"></iframe>
      </section>
    </div>
  </div>

  <script>
    const els = {
      username: document.getElementById("username"),
      tweetInput: document.getElementById("tweet_input"),
      authToken: document.getElementById("auth_token"),
      ct0: document.getElementById("ct0"),
      dbPath: document.getElementById("db_path"),
      taskList: document.getElementById("task_list"),
      status: document.getElementById("metric_status"),
      views: document.getElementById("metric_views"),
      delta: document.getElementById("metric_delta"),
      samples: document.getElementById("metric_samples"),
      statusBox: document.getElementById("status_box"),
      reportFrame: document.getElementById("report_frame"),
      startBtn: document.getElementById("start_btn"),
      stopBtn: document.getElementById("stop_btn"),
      openReportBtn: document.getElementById("open_report_btn"),
      selectedCopy: document.getElementById("selected_copy")
    };

    let selectedTweetId = "";
    let lastReportKey = "";

    const formatNum = (value) => {
      if (value === null || value === undefined || value === "") return "-";
      const num = Number(value);
      if (Number.isNaN(num)) return String(value);
      return num.toLocaleString();
    };

    const escapeHtml = (text) => String(text || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");

    const badgeClass = (status) => {
      if (status === "running") return "badge running";
      if (status === "completed") return "badge completed";
      if (status === "stopped" || status === "error") return "badge stopped";
      return "badge";
    };

    function renderTasks(tasks) {
      if (!tasks.length) {
        els.taskList.innerHTML = '<div class="task-card"><div class="task-meta">还没有任务。先在左边新增第一条推文监测。</div></div>';
        return;
      }

      els.taskList.innerHTML = tasks.map((task) => {
        const active = String(task.tweet_id) === String(selectedTweetId) ? "active" : "";
        const latest = task.latest || {};
        return `
          <div class="task-card ${active}" data-tweet-id="${task.tweet_id}">
            <div class="task-head">
              <div class="task-id">${escapeHtml(task.tweet_id)}</div>
              <span class="${badgeClass(task.status)}">${escapeHtml(task.status || "idle")}</span>
            </div>
            <div class="task-meta">
              <div>账号：${escapeHtml(task.username || "-")}</div>
              <div>当前 impression：${escapeHtml(formatNum(latest.view_count))}</div>
              <div>最近增量：${escapeHtml(formatNum(latest.increase_since_last_check))}</div>
            </div>
          </div>
        `;
      }).join("");

      document.querySelectorAll(".task-card[data-tweet-id]").forEach((card) => {
        card.addEventListener("click", () => {
          selectedTweetId = card.dataset.tweetId || "";
          poll();
        });
      });
    }

    function renderSelectedTask(data) {
      const task = data.selected_task || {};
      const state = task.state || {};
      const latest = task.latest || {};

      els.status.textContent = task.status || "待命";
      els.views.textContent = formatNum(latest.view_count);
      els.delta.textContent = formatNum(latest.increase_since_last_check);
      els.samples.textContent = formatNum((state.sample_count || 0));

      if (task.tweet_id) {
        els.selectedCopy.textContent = `当前查看推文 ${task.tweet_id}。这条任务有自己的后台进程、独立日志、独立报告。`;
      } else {
        els.selectedCopy.textContent = "选中一条任务后，这里会显示它的运行状态、关键数字和报告入口。";
      }

      const parts = [];
      if (state.message) parts.push(state.message);
      if (state.tweet_url) parts.push(`推文：${state.tweet_url}`);
      if (state.next_check_at) parts.push(`下次检查：${state.next_check_at}`);
      if (state.report_path) parts.push(`报告：${state.report_path}`);
      if (task.log_path) parts.push(`日志：${task.log_path}`);

      const badgeText = task.status === "error" ? "有问题" : task.status === "running" ? "运行中" : "状态";
      els.statusBox.innerHTML = `
        <div><span class="${badgeClass(task.status)}">${badgeText}</span></div>
        <div style="margin-top:8px;">${parts.length ? parts.map(escapeHtml).join("<br>") : "这条任务还没有可展示的状态。"}</div>
      `;

      const reportHref = selectedTweetId ? `/report/current?tweet_id=${encodeURIComponent(selectedTweetId)}` : "/report/current";
      els.openReportBtn.setAttribute("href", reportHref);

      if (data.report_available && selectedTweetId) {
        const reportKey = `${selectedTweetId}|${state.sample_count || 0}|${task.status || ""}`;
        if (reportKey !== lastReportKey) {
          lastReportKey = reportKey;
          els.reportFrame.setAttribute("src", `${reportHref}&t=${Date.now()}`);
        }
      }
    }

    async function loadStatus() {
      const suffix = selectedTweetId ? `?tweet_id=${encodeURIComponent(selectedTweetId)}` : "";
      const res = await fetch(`/api/status${suffix}`);
      return await res.json();
    }

    async function loadConfigAndStatus() {
      const data = await loadStatus();
      const cfg = data.config || {};
      selectedTweetId = data.selected_tweet_id || cfg.selected_tweet_id || "";
      els.username.value = cfg.username || "";
      els.tweetInput.value = cfg.tweet_input || "";
      els.authToken.value = cfg.auth_token || "";
      els.ct0.value = cfg.ct0 || "";
      els.dbPath.value = cfg.db_path || "accounts.db";
      renderTasks(data.tasks || []);
      renderSelectedTask(data);
    }

    async function poll() {
      try {
        const data = await loadStatus();
        if (!selectedTweetId && data.selected_tweet_id) {
          selectedTweetId = data.selected_tweet_id;
        }
        renderTasks(data.tasks || []);
        renderSelectedTask(data);
      } catch (err) {
        els.statusBox.innerHTML = `<div><span class="badge stopped">有问题</span></div><div style="margin-top:8px;">状态刷新失败：${escapeHtml(err)}</div>`;
      }
    }

    async function startMonitor() {
      const payload = {
        username: els.username.value.trim(),
        tweet_input: els.tweetInput.value.trim(),
        auth_token: els.authToken.value.trim(),
        ct0: els.ct0.value.trim(),
        db_path: els.dbPath.value.trim() || "accounts.db"
      };

      const res = await fetch("/api/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (!res.ok) {
        els.statusBox.innerHTML = `<div><span class="badge stopped">有问题</span></div><div style="margin-top:8px;">${escapeHtml(data.error || "启动失败")}</div>`;
        return;
      }
      selectedTweetId = data.tweet_id || selectedTweetId;
      await poll();
    }

    async function stopMonitor() {
      if (!selectedTweetId) {
        els.statusBox.innerHTML = `<div><span class="badge">状态</span></div><div style="margin-top:8px;">先在任务列表里选中一条推文。</div>`;
        return;
      }
      const res = await fetch("/api/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tweet_id: selectedTweetId })
      });
      const data = await res.json();
      if (!res.ok) {
        els.statusBox.innerHTML = `<div><span class="badge stopped">有问题</span></div><div style="margin-top:8px;">${escapeHtml(data.error || "停止失败")}</div>`;
        return;
      }
      await poll();
    }

    els.startBtn.addEventListener("click", startMonitor);
    els.stopBtn.addEventListener("click", stopMonitor);

    loadConfigAndStatus();
    setInterval(poll, 5000);
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send_headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.end_headers()

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _html(self, status: int, body: str) -> None:
        encoded = body.encode("utf-8")
        self._send_headers(status, "text/html; charset=utf-8", len(encoded))
        self.wfile.write(encoded)

    def do_HEAD(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/report/current"):
            self._send_headers(200, "text/html; charset=utf-8", 0)
            return
        if parsed.path == "/api/status":
            self._send_headers(200, "application/json; charset=utf-8", 0)
            return
        self._send_headers(404, "application/json; charset=utf-8", 0)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if parsed.path == "/":
            self._html(200, html_page())
            return

        if parsed.path == "/api/status":
            self._json(200, status_payload(query))
            return

        if parsed.path == "/report/current":
            tweet_id = resolve_selected_tweet_id(query)
            report_path = task_report_path(tweet_id) if tweet_id else latest_report_path()
            if report_path and report_path.exists() and report_path.is_file():
                self._html(200, report_path.read_text(encoding="utf-8"))
            else:
                self._html(
                    200,
                    "<!doctype html><html><body style='font-family:sans-serif;padding:24px;color:#475569;'>"
                    "还没有生成报告。先开始一次监测，图表和表格就会在这里出现。"
                    "</body></html>",
                )
            return

        self._json(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"

        if self.headers.get("Content-Type", "").startswith("application/json"):
            payload = json.loads(raw.decode("utf-8") or "{}")
        else:
            payload = {k: v[0] for k, v in parse_qs(raw.decode("utf-8")).items()}

        try:
            if parsed.path == "/api/start":
                self._json(200, start_monitor(payload))
                return
            if parsed.path == "/api/stop":
                self._json(200, stop_monitor((payload.get("tweet_id") or "").strip()))
                return
            self._json(404, {"error": "Not found"})
        except Exception as exc:
            self._json(400, {"error": str(exc)})

    def log_message(self, format, *args):
        return


def pick_port(start=8765) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("8765-8784 之间都被占用了，请先关闭一个本地服务。")


def parse_args():
    parser = argparse.ArgumentParser(description="Local monitor panel for tweet impressions")
    parser.add_argument("--port", type=int, default=8765, help="Preferred local port")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open browser")
    return parser.parse_args()


def main():
    args = parse_args()
    port = pick_port(args.port)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    APP_STATE["server_url"] = f"http://127.0.0.1:{port}"
    save_server_info(APP_STATE["server_url"], port)
    print(f"面板已启动：{APP_STATE['server_url']}", flush=True)
    if args.no_browser:
        print("当前为后台模式：关闭这个终端不会影响面板。", flush=True)
    else:
        print("关闭这个终端窗口会一起关闭面板。", flush=True)
        try:
            webbrowser.open(APP_STATE["server_url"])
        except Exception as exc:
            print(f"自动打开浏览器失败：{exc}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
