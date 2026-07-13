#!/usr/bin/env python3
import argparse
import asyncio
import csv
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from twscrape import API, ConnectError, NetworkError, gather


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_tweet_id(value: str) -> int:
    value = (value or "").strip()
    if not value:
        raise ValueError("推文链接或推文 ID 不能为空。")

    if value.isdigit():
        return int(value)

    match = re.search(r"/status/(\d+)", value)
    if match:
        return int(match.group(1))

    raise ValueError("无法从输入内容里识别推文 ID。请传入纯数字 ID 或完整推文链接。")


def next_interval(age: timedelta) -> int | None:
    if age < timedelta(hours=1):
        return 60
    if age < timedelta(hours=2):
        return 5 * 60
    if age < timedelta(hours=6):
        return 10 * 60
    if age < timedelta(hours=12):
        return 30 * 60
    if age < timedelta(hours=24):
        return 60 * 60
    if age < timedelta(hours=72):
        return 4 * 60 * 60
    return None


def seconds_until_next_check(age: timedelta) -> int | None:
    interval = next_interval(age)
    if interval is None:
        return None

    age_seconds = max(int(age.total_seconds()), 0)
    next_mark = ((age_seconds // interval) + 1) * interval
    return max(next_mark - age_seconds, 1)


def make_log_paths(tweet_id: int) -> dict[str, Path]:
    out_dir = Path("impression_logs")
    out_dir.mkdir(exist_ok=True)
    return {
        "csv": out_dir / f"tweet_{tweet_id}_impressions.csv",
        "jsonl": out_dir / f"tweet_{tweet_id}_impressions.jsonl",
        "report": out_dir / f"tweet_{tweet_id}_report.html",
        "state": out_dir / f"tweet_{tweet_id}_state.json",
    }


def append_csv(path: Path, row: dict) -> None:
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(row.keys()))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_history(path: Path) -> list[dict]:
    if not path.exists():
        return []

    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def last_known_views(rows: list[dict]) -> int | None:
    for row in reversed(rows):
        value = row.get("view_count")
        if isinstance(value, int):
            return value
    return None


def fmt_num(value) -> str:
    if value is None:
        return "-"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def svg_line_chart(values: list[int | None], width=760, height=240) -> str:
    clean = [x for x in values if isinstance(x, int)]
    if not clean:
        return '<div class="empty">还没有可用的总量数据</div>'

    min_v = min(clean)
    max_v = max(clean)
    span = max(max_v - min_v, 1)
    step_x = width / max(len(values) - 1, 1)

    points = []
    for idx, value in enumerate(values):
        if value is None:
            continue
        x = idx * step_x
        y = height - ((value - min_v) / span) * (height - 24) - 12
        points.append(f"{x:.1f},{y:.1f}")

    grid = []
    for i in range(5):
        y = 12 + i * ((height - 24) / 4)
        grid.append(
            f'<line x1="0" y1="{y:.1f}" x2="{width}" y2="{y:.1f}" '
            'stroke="rgba(148,163,184,0.25)" stroke-width="1" />'
        )

    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart-svg">'
        + "".join(grid)
        + f'<polyline fill="none" stroke="#2563eb" stroke-width="3" points="{" ".join(points)}" />'
        + "</svg>"
    )


def svg_bar_chart(values: list[int | None], width=760, height=240) -> str:
    clean = [x for x in values if isinstance(x, int)]
    if not clean:
        return '<div class="empty">还没有可用的增量数据</div>'

    max_v = max(clean)
    bar_width = max(width / max(len(values), 1) - 6, 4)
    step_x = width / max(len(values), 1)
    bars = []

    for idx, value in enumerate(values):
        if value is None:
            continue
        bar_h = ((value / max_v) * (height - 24)) if max_v else 0
        x = idx * step_x + 3
        y = height - bar_h - 12
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_h:.1f}" '
            'rx="4" fill="#0f766e" />'
        )

    grid = []
    for i in range(5):
        y = 12 + i * ((height - 24) / 4)
        grid.append(
            f'<line x1="0" y1="{y:.1f}" x2="{width}" y2="{y:.1f}" '
            'stroke="rgba(148,163,184,0.25)" stroke-width="1" />'
        )

    return f'<svg viewBox="0 0 {width} {height}" class="chart-svg">{"".join(grid)}{"".join(bars)}</svg>'


def generate_report(rows: list[dict], report_path: Path, tweet_meta: dict) -> None:
    latest = rows[-1] if rows else {}
    first = rows[0] if rows else {}
    values = [row.get("view_count") if isinstance(row.get("view_count"), int) else None for row in rows]
    deltas = [
        row.get("increase_since_last_check") if isinstance(row.get("increase_since_last_check"), int) else None
        for row in rows
    ]
    valid_values = [x for x in values if isinstance(x, int)]
    valid_deltas = [x for x in deltas if isinstance(x, int)]
    latest_views = latest.get("view_count") if isinstance(latest.get("view_count"), int) else None
    first_views = first.get("view_count") if isinstance(first.get("view_count"), int) else None
    net_gain = latest_views - first_views if latest_views is not None and first_views is not None else None
    peak_delta = max(valid_deltas) if valid_deltas else None
    avg_delta = round(sum(valid_deltas) / len(valid_deltas)) if valid_deltas else None
    peak_views = max(valid_values) if valid_values else None
    latest_delta = latest.get("increase_since_last_check")
    latest_checked_at = html.escape(str(latest.get("checked_at", "-")))
    sample_count = len(rows)
    capture_window = "-"
    if rows:
        capture_window = (
            f"{html.escape(str(first.get('checked_at', '-')))}"
            f" to {html.escape(str(latest.get('checked_at', '-')))}"
        )

    table_rows = []
    for row in reversed(rows[-30:]):
        table_rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('checked_at', '-')))}</td>"
            f"<td>{fmt_num(row.get('view_count'))}</td>"
            f"<td>{fmt_num(row.get('increase_since_last_check'))}</td>"
            f"<td>{fmt_num(row.get('like_count'))}</td>"
            f"<td>{fmt_num(row.get('retweet_count'))}</td>"
            "</tr>"
        )

    title = f"Tweet {tweet_meta.get('tweet_id', '')} Impression Report"
    tweet_url = html.escape(str(tweet_meta.get("tweet_url", "")))
    created_at = html.escape(str(tweet_meta.get("tweet_created_at", "-")))
    summary = (
        f"As of {latest_checked_at}, this tweet reached {fmt_num(latest_views)} impressions. "
        f"Since the first captured checkpoint, it added {fmt_num(net_gain)} impressions, "
        f"with the strongest observed interval at {fmt_num(peak_delta)}."
    )

    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f3f5f8;
      --panel: #ffffff;
      --text: #0f172a;
      --muted: #475569;
      --line: #dbe2ea;
      --accent: #0f172a;
      --accent-2: #0f766e;
      --accent-3: #c2410c;
      --band: #eaf1fb;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top right, rgba(15, 118, 110, 0.08), transparent 24%),
        linear-gradient(180deg, #eef4ff 0%, var(--bg) 260px);
      color: var(--text);
    }}
    .wrap {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 28px 20px 56px;
    }}
    .hero {{
      display: grid;
      gap: 14px;
      padding: 28px 28px 24px;
      background: linear-gradient(135deg, rgba(255,255,255,0.92), rgba(234,241,251,0.92));
      border: 1px solid rgba(148, 163, 184, 0.24);
      border-radius: 8px;
      margin-bottom: 18px;
    }}
    .hero h1 {{
      margin: 0;
      font-size: 38px;
      line-height: 1.02;
      max-width: 720px;
    }}
    .hero-copy {{
      font-size: 18px;
      line-height: 1.5;
      max-width: 820px;
    }}
    .meta {{
      color: var(--muted);
      font-size: 14px;
    }}
    .meta-row {{
      display: flex;
      gap: 18px;
      flex-wrap: wrap;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px 18px 20px;
      box-shadow: 0 10px 24px rgba(15, 23, 42, 0.04);
    }}
    .label {{
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 8px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    .value {{
      font-size: 32px;
      font-weight: 700;
    }}
    .subvalue {{
      margin-top: 8px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    .section {{
      margin-bottom: 18px;
    }}
    .section-head {{
      display: flex;
      justify-content: space-between;
      align-items: end;
      gap: 16px;
      margin-bottom: 10px;
    }}
    .section-title {{
      font-size: 18px;
      font-weight: 700;
    }}
    .section-note {{
      color: var(--muted);
      font-size: 13px;
    }}
    .insight-grid {{
      display: grid;
      grid-template-columns: 1.15fr 0.85fr;
      gap: 12px;
      margin-bottom: 18px;
    }}
    .brief {{
      background: linear-gradient(135deg, rgba(15,23,42,0.96), rgba(30,41,59,0.96));
      color: white;
      border-radius: 8px;
      padding: 22px 22px 24px;
    }}
    .brief-label {{
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: rgba(255,255,255,0.72);
      margin-bottom: 10px;
    }}
    .brief-copy {{
      font-size: 22px;
      line-height: 1.4;
      margin: 0;
    }}
    .snapshot {{
      background: var(--band);
      border-radius: 8px;
      padding: 18px 20px;
      border: 1px solid rgba(148, 163, 184, 0.2);
    }}
    .snapshot-row {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 0;
      border-bottom: 1px solid rgba(148, 163, 184, 0.25);
    }}
    .snapshot-row:last-child {{
      border-bottom: 0;
      padding-bottom: 0;
    }}
    .snapshot-key {{
      color: var(--muted);
      font-size: 13px;
    }}
    .snapshot-value {{
      font-size: 16px;
      font-weight: 700;
      text-align: right;
    }}
    .charts {{
      display: grid;
      gap: 12px;
      grid-template-columns: 1fr 1fr;
      margin-bottom: 18px;
    }}
    .chart-svg {{
      width: 100%;
      height: auto;
      display: block;
    }}
    .empty {{
      min-height: 240px;
      display: grid;
      place-items: center;
      color: var(--muted);
      background: #f8fafc;
      border-radius: 6px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    th, td {{
      text-align: left;
      padding: 10px 8px;
      border-bottom: 1px solid var(--line);
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    a {{ color: var(--accent); text-decoration: none; }}
    @media (max-width: 920px) {{
      .insight-grid, .charts {{
        grid-template-columns: 1fr;
      }}
      .hero h1 {{
        font-size: 32px;
      }}
      .brief-copy {{
        font-size: 18px;
      }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <h1>Tweet Impression Brief</h1>
      <div class="hero-copy">{html.escape(summary)}</div>
      <div class="meta-row">
        <div class="meta">推文链接：<a href="{tweet_url}">{tweet_url}</a></div>
        <div class="meta">推文发布时间：{created_at}</div>
        <div class="meta">最近更新时间：{latest_checked_at}</div>
      </div>
    </section>

    <section class="insight-grid">
      <div class="brief">
        <div class="brief-label">Meeting Readout</div>
        <p class="brief-copy">{html.escape(summary)}</p>
      </div>
      <div class="snapshot">
        <div class="snapshot-row">
          <div class="snapshot-key">监测区间</div>
          <div class="snapshot-value">{capture_window}</div>
        </div>
        <div class="snapshot-row">
          <div class="snapshot-key">峰值总量</div>
          <div class="snapshot-value">{fmt_num(peak_views)}</div>
        </div>
        <div class="snapshot-row">
          <div class="snapshot-key">平均单次增量</div>
          <div class="snapshot-value">{fmt_num(avg_delta)}</div>
        </div>
        <div class="snapshot-row">
          <div class="snapshot-key">最近一次增量</div>
          <div class="snapshot-value">{fmt_num(latest_delta)}</div>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="section-head">
        <div class="section-title">Key Metrics</div>
        <div class="section-note">先看结果，再看过程</div>
      </div>
      <div class="grid">
      <div class="panel">
        <div class="label">Current Impressions</div>
        <div class="value">{fmt_num(latest_views)}</div>
        <div class="subvalue">当前抓到的最新总量</div>
      </div>
      <div class="panel">
        <div class="label">Net Gain Since First Capture</div>
        <div class="value">{fmt_num(net_gain)}</div>
        <div class="subvalue">从第一次记录到现在的新增量</div>
      </div>
      <div class="panel">
        <div class="label">Strongest Interval Gain</div>
        <div class="value">{fmt_num(peak_delta)}</div>
        <div class="subvalue">单个采样区间里最高的一次增长</div>
      </div>
      <div class="panel">
        <div class="label">Sample Count</div>
        <div class="value">{fmt_num(sample_count)}</div>
        <div class="subvalue">已累计的采样次数</div>
      </div>
    </section>
    </section>

    <section class="section">
      <div class="section-head">
        <div class="section-title">Trend View</div>
        <div class="section-note">左边看总体爬升，右边看每次新增</div>
      </div>
      <div class="charts">
      <div class="panel">
        <div class="label">Impression Total Trend</div>
        {svg_line_chart(values)}
      </div>
      <div class="panel">
        <div class="label">Impression Increment Trend</div>
        {svg_bar_chart(deltas)}
      </div>
    </section>
    </section>

    <section class="panel">
      <div class="section-head">
        <div class="section-title">Recent Samples</div>
        <div class="section-note">保留最近 30 次，方便会里追问时快速下钻</div>
      </div>
      <table>
        <thead>
          <tr>
            <th>检查时间</th>
            <th>总量</th>
            <th>增量</th>
            <th>点赞</th>
            <th>转发</th>
          </tr>
        </thead>
        <tbody>
          {"".join(table_rows) or '<tr><td colspan="5">还没有数据</td></tr>'}
        </tbody>
      </table>
    </section>
  </div>
</body>
</html>
"""

    report_path.write_text(page, encoding="utf-8")


def write_state(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def retry_delay_seconds(failure_count: int) -> int:
    if failure_count <= 1:
        return 60
    if failure_count == 2:
        return 120
    if failure_count == 3:
        return 300
    return 600


async def pick_latest_tweet(api: API, user_id: int):
    tweets = await gather(api.user_tweets(user_id, limit=1))
    return tweets[0] if tweets else None


async def get_target_tweet(api: API, args):
    if args.tweet:
        return await api.tweet_details(parse_tweet_id(args.tweet))
    return await pick_latest_tweet(api, args.user_id)


async def monitor(args):
    api = API(args.db)
    tweet = await get_target_tweet(api, args)

    if tweet is None:
        print("没有找到要监测的推文。请检查用户 ID / 推文 ID 是否正确。")
        return

    tweet_id = tweet.id
    paths = make_log_paths(tweet_id)
    history = read_history(paths["jsonl"])
    last_views = last_known_views(history)
    tweet_meta = {
        "tweet_id": tweet.id,
        "tweet_url": tweet.url,
        "tweet_created_at": tweet.date.astimezone().isoformat(timespec="seconds"),
    }

    write_state(
        paths["state"],
        {
            "status": "running",
            "tweet_id": tweet_id,
            "tweet_url": tweet.url,
            "tweet_created_at": tweet_meta["tweet_created_at"],
            "csv_path": str(paths["csv"]),
            "jsonl_path": str(paths["jsonl"]),
            "report_path": str(paths["report"]),
            "started_at": now_utc().astimezone().isoformat(timespec="seconds"),
            "sample_count": len(history),
            "latest": history[-1] if history else None,
            "message": "监测已启动",
            "pid": os.getpid(),
        },
    )

    if history:
        generate_report(history, paths["report"], tweet_meta)

    consecutive_failures = 0

    print(f"开始监测推文：{tweet.url}")
    print(f"发布时间：{tweet.date.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"CSV：{paths['csv']}")
    print(f"报告：{paths['report']}")
    print("按 Control + C 可以手动停止。")
    print()

    try:
        while True:
            checked_at = now_utc()
            age = checked_at - tweet.date

            if seconds_until_next_check(age) is None:
                message = "推文已发布超过 72 小时，监测结束。"
                print(message)
                write_state(
                    paths["state"],
                    {
                        "status": "completed",
                        "tweet_id": tweet_id,
                        "tweet_url": tweet.url,
                        "tweet_created_at": tweet_meta["tweet_created_at"],
                        "csv_path": str(paths["csv"]),
                        "jsonl_path": str(paths["jsonl"]),
                        "report_path": str(paths["report"]),
                        "sample_count": len(history),
                        "latest": history[-1] if history else None,
                        "message": message,
                        "pid": os.getpid(),
                    },
                )
                break

            try:
                current = await api.tweet_details(tweet_id)
            except (ConnectError, NetworkError) as exc:
                consecutive_failures += 1
                delay = retry_delay_seconds(consecutive_failures)
                message = (
                    f"网络连接异常，第 {consecutive_failures} 次重试。"
                    f" {delay // 60 if delay >= 60 else delay} "
                    f"{'分钟' if delay >= 60 else '秒'}后重试。"
                )
                print(f"{message} 错误类型：{type(exc).__name__}")
                write_state(
                    paths["state"],
                    {
                        "status": "warning",
                        "tweet_id": tweet_id,
                        "tweet_url": tweet.url,
                        "tweet_created_at": tweet_meta["tweet_created_at"],
                        "csv_path": str(paths["csv"]),
                        "jsonl_path": str(paths["jsonl"]),
                        "report_path": str(paths["report"]),
                        "sample_count": len(history),
                        "latest": history[-1] if history else None,
                        "message": message,
                        "next_check_at": (checked_at + timedelta(seconds=delay)).astimezone().isoformat(timespec="seconds"),
                        "pid": os.getpid(),
                    },
                )
                await asyncio.sleep(delay)
                continue

            if current is None:
                consecutive_failures += 1
                delay = retry_delay_seconds(consecutive_failures)
                message = f"这次没有读取到推文详情，{delay // 60 if delay >= 60 else delay}{'分钟' if delay >= 60 else '秒'}后重试。"
                print(message)
                write_state(
                    paths["state"],
                    {
                        "status": "warning",
                        "tweet_id": tweet_id,
                        "tweet_url": tweet.url,
                        "tweet_created_at": tweet_meta["tweet_created_at"],
                        "csv_path": str(paths["csv"]),
                        "jsonl_path": str(paths["jsonl"]),
                        "report_path": str(paths["report"]),
                        "sample_count": len(history),
                        "latest": history[-1] if history else None,
                        "message": message,
                        "next_check_at": (checked_at + timedelta(seconds=delay)).astimezone().isoformat(timespec="seconds"),
                        "pid": os.getpid(),
                    },
                )
                await asyncio.sleep(delay)
                continue

            consecutive_failures = 0
            views = current.viewCount
            increase = None if last_views is None or views is None else views - last_views
            age = checked_at - current.date

            row = {
                "checked_at": checked_at.astimezone().isoformat(timespec="seconds"),
                "tweet_id": current.id,
                "tweet_url": current.url,
                "tweet_created_at": current.date.astimezone().isoformat(timespec="seconds"),
                "age_minutes": round(age.total_seconds() / 60, 2),
                "view_count": views,
                "increase_since_last_check": increase,
                "reply_count": current.replyCount,
                "retweet_count": current.retweetCount,
                "like_count": current.likeCount,
                "quote_count": current.quoteCount,
                "bookmarked_count": current.bookmarkedCount,
            }

            append_csv(paths["csv"], row)
            append_jsonl(paths["jsonl"], row)
            history.append(row)
            generate_report(history, paths["report"], tweet_meta)

            increase_text = "首次记录" if increase is None else f"+{increase}"
            views_text = "未知" if views is None else str(views)
            print(
                f"{row['checked_at']} | 已发布 {row['age_minutes']} 分钟 | "
                f"impression/views 总量：{views_text} | 本次增量：{increase_text}"
            )

            if views is not None:
                last_views = views

            wait_seconds = seconds_until_next_check(age)
            next_time = (checked_at + timedelta(seconds=wait_seconds)).astimezone()
            message = f"下次监测：{next_time.strftime('%Y-%m-%d %H:%M:%S %Z')}"
            print(message)
            write_state(
                paths["state"],
                {
                    "status": "running",
                    "tweet_id": tweet_id,
                    "tweet_url": current.url,
                    "tweet_created_at": tweet_meta["tweet_created_at"],
                    "csv_path": str(paths["csv"]),
                    "jsonl_path": str(paths["jsonl"]),
                    "report_path": str(paths["report"]),
                    "sample_count": len(history),
                    "latest": row,
                    "next_check_at": next_time.isoformat(timespec="seconds"),
                    "message": message,
                    "pid": os.getpid(),
                },
            )
            await asyncio.sleep(wait_seconds)
    except KeyboardInterrupt:
        write_state(
            paths["state"],
            {
                "status": "stopped",
                "tweet_id": tweet_id,
                "tweet_url": tweet.url,
                "tweet_created_at": tweet_meta["tweet_created_at"],
                "csv_path": str(paths["csv"]),
                "jsonl_path": str(paths["jsonl"]),
                "report_path": str(paths["report"]),
                "sample_count": len(history),
                "latest": history[-1] if history else None,
                "message": "用户手动停止监测。",
                "pid": os.getpid(),
            },
        )
        raise
    except Exception as exc:
        write_state(
            paths["state"],
            {
                "status": "error",
                "tweet_id": tweet_id,
                "tweet_url": tweet.url,
                "tweet_created_at": tweet_meta["tweet_created_at"],
                "csv_path": str(paths["csv"]),
                "jsonl_path": str(paths["jsonl"]),
                "report_path": str(paths["report"]),
                "sample_count": len(history),
                "latest": history[-1] if history else None,
                "message": f"监测失败：{exc}",
                "pid": os.getpid(),
            },
        )
        raise


def parse_args():
    parser = argparse.ArgumentParser(
        description="Monitor one X/Twitter tweet's public impression/views count."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--user-id", type=int, help="你的用户 ID；脚本会自动监测最新一条推文")
    target.add_argument("--tweet", help="推文链接或推文 ID")
    target.add_argument("--tweet-id", dest="tweet", help="兼容旧参数：推文链接或推文 ID")
    parser.add_argument("--db", default="accounts.db", help="twscrape 账号数据库，默认 accounts.db")
    return parser.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(monitor(parse_args()))
    except KeyboardInterrupt:
        print("\n已手动停止监测。")
