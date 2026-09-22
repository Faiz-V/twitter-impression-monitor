# Twitter Impression Monitor

A local Python panel for periodically recording public X post view counts and producing CSV, JSONL and HTML reports.

中文简介：这是一个个人运营用的本地曝光记录工具，以 `twscrape` 读取单条帖子的公开指标；不是 X 官方 Analytics 或企业分析服务。

**Status: historical utility.** The panel can be started locally, but live X collection depends on valid user-supplied sessions and changeable upstream endpoints. No current end-to-end collection guarantee is made.

## What it does

- Starts a separate monitoring process for each post URL/ID and displays local task status.
- Samples based on the post's age: every minute in the first hour, then every 5, 10 and 30 minutes, every hour, and every 4 hours up to 72 hours after publication.
- Writes snapshots and reports under `impression_logs/`. An older post does not receive a new 72-hour monitoring window.
- Uses the vendored `twscrape` package to request public post data. It does not provide private owner analytics, unique reach, ad reporting or guaranteed complete data.

## Install and start

Requirements: Python 3.10+ and a modern browser. macOS/Linux is the intended monitoring environment: task stopping uses POSIX process groups (`os.killpg`); macOS also optionally uses `caffeinate` to keep a monitoring task awake. Native Windows panel startup has been checked, but its full collection/stop lifecycle is not supported by this verification.

From the repository root on macOS/Linux:

```sh
git clone https://github.com/Faiz-V/twitter-impression-monitor.git
cd twitter-impression-monitor
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ./twscrape-main
export TWS_TELEMETRY=0
python monitor_panel.py --no-browser --port 8765
```

For a native Windows **panel-only preview**, first clone the repository and open its root directory as above, then run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .\twscrape-main
$env:TWS_TELEMETRY = "0"
.\.venv\Scripts\python.exe monitor_panel.py --no-browser --port 8765
```

Open the URL printed by the program. It binds to `127.0.0.1` and chooses another port if the preferred port is occupied. The empty panel can be viewed without credentials. Before closing the panel, stop any monitoring tasks through the panel; closing the parent process is not a reliable way to stop detached child tasks.

The legacy `.command` and shell launchers contain the original author's absolute macOS path and expect another virtual-environment location. Use the commands above instead; those launchers are not portable installation instructions.

## Data source and usage

The panel accepts a username, `auth_token`, `ct0` and post URL/ID. Those cookies are login credentials, not public API keys. Use only your own authorized session and data access; do not put credentials in an issue, screenshot, command example or repository. No credentials are supplied here.

The monitor uses X's internal web endpoints through `twscrape`, not the official X API. Endpoint changes, account restrictions, rate limits, deleted/protected posts or expired sessions can stop collection. Sleeping or disconnecting the machine also affects sampling. Public view counts are not unique audience counts and cannot reconstruct missed historical snapshots.

## Privacy and local files

| Location | Contents |
|---|---|
| `.monitor_panel_config.json` | Username and session cookies stored as **plaintext JSON** |
| `accounts.db` (or the path you choose) | The `twscrape` account/session database |
| `monitor_panel_runtime/` | Task records, process IDs and logs |
| `impression_logs/` | Per-post CSV, JSONL, HTML and state files |

These default paths are ignored by Git, but ignore rules are not encryption or access control. A custom database path may fall outside those rules. The local status API returns the saved configuration to the panel, so keep the service on loopback and do not expose it through a public proxy.

The vendored `twscrape` includes upstream PostHog telemetry. Set **`TWS_TELEMETRY=0`** (as above), or `DO_NOT_TRACK=1`, to disable it. Without that setting it can send usage events and a hashed machine identifier to the upstream telemetry service. Monitoring itself still requires network access to X. Do not describe this application as completely offline.

## Implementation and third-party boundary

- `monitor_panel.py`: standard-library HTTP panel and task orchestration.
- `twscrape-main/scripts/monitor_impressions.py`: post-age sampling and report generation.
- `twscrape-main/twscrape/`: vendored third-party library; its package metadata identifies `twscrape` **0.19.0** and [vladkens/twscrape](https://github.com/vladkens/twscrape) as upstream.
- `twscrape-main/tests/`: upstream-oriented tests and captured fixtures; they are not an independent test suite for the local panel.

The root [MIT license](LICENSE) does not replace the [upstream MIT notice](twscrape-main/LICENSE). Keep the upstream attribution. Vendored tests/examples and public-post fixtures should not be represented as original user-owned implementation or private operational results.

## Verification and limitations

On 2026-09-22, installation from `./twscrape-main`, the empty loopback panel and `/api/status` were checked in an isolated Windows environment with telemetry disabled and no X credentials. Live collection, a 72-hour run, macOS launchers and native Windows task stopping were not verified.

There is no root GitHub Actions workflow or packaged Release. Workflows inside `twscrape-main/.github/` are vendored files and do not run as this repository's CI. Dependency versions beyond the vendored package are not fully pinned by the simple pip command.

[Report an issue](https://github.com/Faiz-V/twitter-impression-monitor/issues) with your Python/OS version and a redacted error. Never attach account databases, session cookies or private monitoring reports.
