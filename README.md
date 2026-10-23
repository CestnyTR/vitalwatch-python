# vitalwatch (Python)

Python reporter for [VitalWatch Hub](https://github.com/CestnyTR/vitalwatch-hub): server
errors, slow requests, heartbeat. Also ships
[`vitalwatch-agent`](#server-agent-vitalwatch-agent), which watches the server itself: web
server, PHP-FPM, databases, systemd, Docker, OOM and disk.

No dependencies. No build step. Python 3.9+.

Sibling packages: `cestnytr/vitalwatch-laravel` (Laravel), `vitalwatch-node` (Node),
`t.js` (browser, in the hub repo at `resources/sdk/t.js`).

---

## Scope — and why it is this narrow

This package covers **the server side only**. Browser errors are collected by `t.js`,
served from the hub; that file was deliberately not moved into a package — being hosted
means a fix reaches every site within minutes, whereas a package would require
redeploying every project for each fix.

Never collected — not even via configuration:

IP address · User-Agent · request body · query string **values** (path only) ·
session data · cookies · raw SQL literals

---

## Installation

```bash
pip install vitalwatch
```

In your `.env` file — the same variable names as the sibling packages:

```env
VITALWATCH_ENABLED=true
VITALWATCH_URL=https://vitalwatch.example.com
VITALWATCH_KEY=project-key
VITALWATCH_SECRET=<64-character secret from the dashboard>
VITALWATCH_ENV=production
```

### Flask

```python
from flask import Flask
from vitalwatch.flask import VitalWatchFlask

app = Flask(__name__)
VitalWatchFlask(app)
```

That one line installs: uncaught exceptions (`teardown_request`), 5xx and slow
requests (`after_request`), process-level hooks (`sys.excepthook`,
`threading.excepthook`) and an 8-hour heartbeat.

- **One failure, one record:** a failing request produces only an `exception` with a
  stack trace; no separate `http_5xx` is sent for the same request (0.1.1+).
- **If you have your own error handler:** with `@app.errorhandler(Exception)` or
  `errorhandler(500)` defined, the error still arrives with its stack trace. If the
  handler returns a 4xx, nothing is sent. Previously the error was lost in this case and
  only a stack-less `http_5xx` arrived.
- **`abort(4xx)` is not sent:** it is a deliberate response, not a failure.
- **Setup order does not matter:** `VitalWatchFlask(app)` can be called first and
  `vitalwatch.init(...)` later; both the request hooks and the process hooks
  (`sys.excepthook`, `threading.excepthook`) resolve the current reporter at call time.
  If there is no configuration at setup time, the heartbeat starts once `init(...)` is
  called.
- **Installing twice is harmless:** a second `VitalWatchFlask(app)` on the same app adds
  nothing; process hooks are installed once per process.
- **Never crashes your app:** setup (including a broken or non-UTF-8 `.env`) never
  raises under any circumstances.
- **Disabling process hooks:** `VitalWatchFlask(app, hook_process_errors=False)`
  installs the request hooks only.

### Django

```python
# settings.py
MIDDLEWARE = [
    "vitalwatch.django.VitalWatchMiddleware",  # near the top of the list
    # ...
]
```

The middleware times slow and 5xx requests and sends uncaught exceptions with their
stack trace. Errors are seen through two paths: the `got_request_exception` signal
Django emits while turning an exception into a 500 (which also covers errors raised in
lower middleware, not just the view) and `process_exception`. Same rules as Flask:

- **One failure, one record;** no separate `http_5xx` for a failing request.
- **4xx is not sent:** `Http404`, `PermissionDenied`, `BadRequest`,
  `SuspiciousOperation`.
- **The route pattern is sent:** `/product/<int:product_id>/`, not the real id. For
  `re_path` patterns the leading `^` and trailing `$` are stripped.
- **Errors are never swallowed:** `process_exception` returns `None`; Django's 500 page
  and `handler500` work as usual.
- **Process hooks** are enabled once on first setup; set
  `VITALWATCH_HOOK_PROCESS = False` to disable them.

### FastAPI / Starlette (ASGI)

```python
from fastapi import FastAPI
from vitalwatch.asgi import VitalWatchASGIMiddleware

app = FastAPI()
app.add_middleware(VitalWatchASGIMiddleware)
```

The same line works in Starlette (as does
`Starlette(middleware=[Middleware(VitalWatchASGIMiddleware)])`). Any other ASGI app can be
wrapped directly: `app = VitalWatchASGIMiddleware(app)`. It is a pure ASGI middleware;
Starlette/FastAPI are not imported at import time.

- **One failure, one record;** 4xx `HTTPException` and validation errors (422) are not
  sent. A deliberate `HTTPException(5xx)` is recorded as a stack-less `http_5xx`.
- **The route pattern is sent:** `/product/{product_id}`; the `include_router` prefix
  and `Mount` path are prepended to the pattern.
- **If you have your own error handler:** `@app.exception_handler(Exception)` still
  routes the error through our middleware. A handler bound to a specific class
  (`@app.exception_handler(ValueError)`) that returns 5xx still delivers the error with
  its stack trace; one that returns 4xx sends nothing. Middleware order does not matter
  (anywhere in `add_middleware` or `middleware=[...]`). For a manually wrapped app
  (`VitalWatchASGIMiddleware(app)`) this kicks in after the first request, because
  Starlette builds its stack on the first request; during that first request a 5xx
  becomes a stack-less `http_5xx`.
- **Timing stops at the last byte of the response:** `BackgroundTasks` and post-response
  work do not count as slow requests. Long streams (SSE, large downloads) run until the
  last chunk and may be counted as slow.
- **Nested middleware records once:** if a `Mount`ed sub-application also has
  `VitalWatchASGIMiddleware`, the request is timed only by the outer one.
- **Errors are never swallowed:** they are reported and re-raised; lifespan and
  websocket pass through untouched.
- **Disabling process hooks:**
  `app.add_middleware(VitalWatchASGIMiddleware, hook_process_errors=False)`.

### Without a framework

```python
import vitalwatch

vitalwatch.hook_process()          # hooks + heartbeat
vitalwatch.report(error)           # manual report

vitalwatch.init(ignore=(KeyboardInterrupt, "MyExpectedError"))  # class or name
vitalwatch.start_heartbeat()       # heartbeat only (hook_process already starts it)
vitalwatch.stop_heartbeat()
```

`init()` reads environment variables; arguments you pass override them. If it is never
called, it sets itself up on first use.

### Verify the installation

```bash
vitalwatch-status
```

Do not skip this step. The package's most expensive failure mode is **silently not
working**: if the secret was copied incompletely, nothing breaks and nothing is logged —
the hub answers an invalid signature with `204` too. An installation can be dead for
months and that silence is mistaken for "all good".

```bash
vitalwatch-status --test         # a real test event — shows up as an error in the dashboard
vitalwatch-status --vitalwatch   # heartbeat only — does not create an error in the dashboard
```

`--test` is the right choice when verifying a single installation. **In an update loop
that goes through dozens of installations, use `--vitalwatch`** — `--test` would leave
dozens of fake errors in the dashboard, i.e. the monitoring tool would generate its own
noise.

Only the **hub dashboard** can confirm that data actually arrives: the connection status
on the project row should read `Connected`. The command cannot tell this on its
own, because the hub answers valid and invalid signatures identically to the outside.

Example output:

```text
  SDK version        0.2.3
  Latest version     0.2.3
  Enabled            yes
  Hub URL            https://hub.example.com
  Project key        my-project
  Secret length      64 characters
  Environment        production
  Python             3.12.3
  Release tag        abc123def456 (source: .git)

  ✓ Configuration OK.

  Only the hub panel can confirm that data arrives:
  the project row should show the connection status "Connected".
  The hub returns 204 for an invalid signature too, so this command cannot tell.
```

The command also shows:

- **`Release tag`** — the `release` attached to events and where it was found:
  `abc123def456 (source: .git)`; the source is `VITALWATCH_RELEASE`, the name of a CI/PaaS
  variable, `.git` or `none` (the tag itself then reads `not set`). Details:
  [Release tag](#release-tag-release).
- **`Latest version`** — the newest stable version on PyPI (`info.version`). If it is
  newer than the installed version, an `! Update available: pip install -U vitalwatch`
  line is printed; this is a warning and does not change the exit code. Without network
  access, or if PyPI does not respond, it prints `could not check` (2 s timeout). To never
  touch the network, set `VITALWATCH_STATUS_OFFLINE=1` (`1`, `true`, `yes`, `on`, `evet`;
  case-insensitive) — the `Latest version` line is then omitted entirely.

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `VITALWATCH_ENABLED` | `true` | When `false`, no data is sent |
| `VITALWATCH_URL` | — | Hub URL (https) |
| `VITALWATCH_KEY` | — | Project key |
| `VITALWATCH_SECRET` | — | 64-character signing secret |
| `VITALWATCH_ENV` | `APP_ENV` | `production` · `staging` · `local`. Aliases are mapped: `prod`/`live` → production, `stage`/`stg`/`preprod`/`uat` → staging, `dev`/`development`/`test`/`testing` → local. The hub rejects unknown values; the diagnostic command reports an error |
| `VITALWATCH_RELEASE` | auto-detected | Release tag. If empty, it is taken from CI/PaaS commit variables or from `.git` — see below |
| `VITALWATCH_STATUS_OFFLINE` | — | When `1`/`true`/`yes`/`on`/`evet`, `vitalwatch-status` does not go online for the update check |
| `VITALWATCH_TIMEOUT` | `2.0` | Send timeout in **seconds**. Values of 100 and above are treated as milliseconds — Node expects ms under the same name, so `2000` should not mean 2000 seconds. Invalid, zero/negative, NaN or infinite → 2 s; the result is clamped to 0.1–10 s |
| `VITALWATCH_SLOW_REQUEST_MS` | `1000` | Slow request threshold |

Variables are read from `os.environ` and from the `.env` / `.env.local` / `.env.<APP_ENV>`
files; the process environment always wins. Only keys prefixed with `VITALWATCH_` are
read.

### Release tag (`release`)

`VITALWATCH_RELEASE` was left empty in almost every installation, so the hub could not
tell **which deploy an error started with**. Our deploys are mostly `git pull` + a
gunicorn restart on the server: the new process reads the commit itself on startup, and
the first error after a deploy shows the new release in the dashboard. The tag is
resolved once at setup (not per event); the first non-empty source wins:

1. A value passed explicitly via `init(release=...)`.
2. `VITALWATCH_RELEASE` — trimmed, at most 64 characters, used as is.
3. In order: `GIT_COMMIT`, `GIT_SHA`, `COMMIT_SHA`, `SOURCE_VERSION`,
   `VERCEL_GIT_COMMIT_SHA`, `RENDER_GIT_COMMIT`, `HEROKU_SLUG_COMMIT`, `CI_COMMIT_SHA`.
   If the value is 7–40 hex digits, the first 12 in lowercase; otherwise trimmed, at most
   64 characters.
4. The git checkout in the working directory: `.git/HEAD` → the branch ref file, falling
   back to `packed-refs`; on a detached HEAD, the sha itself. If `.git` is a file
   (submodules etc.), `gitdir:` is followed. Only a valid 40-digit sha is accepted; the
   first 12 digits in lowercase. The `git` command is never run — files are read; if they
   cannot be read, it silently gives up. `ref:` may only point under `refs/` and may not
   contain `..`. **Git worktrees (`commondir`) are not supported** — the branch ref lives
   in the common directory and is not found; use `VITALWATCH_RELEASE` there.
5. If none of these is available, no tag is sent.

The rule is the same as in the Node and Laravel packages. If gunicorn's working
directory is not the application root (no `--chdir`), step 4 finds nothing;
running `vitalwatch-status` from the same directory shows this as `(source: none)`.

---

## What gets reported

| Type | When |
|---|---|
| `exception` | Uncaught exception, `report()` call |
| `fatal` | `MemoryError`, `RecursionError`, `SystemError` — hitting the interpreter's limits |
| `http_5xx` | Response status ≥ 500 |
| `slow_request` | Duration ≥ `VITALWATCH_SLOW_REQUEST_MS` |

Successful, fast requests are **not reported**: sending every request would burden both
the monitored application and the hub.

---

## Server agent (`vitalwatch-agent`)

The SDK looks from **inside** the application. Failures outside it — nginx `502/504`s,
PHP-FPM hitting `pm.max_children`, PostgreSQL `FATAL`, a crashed systemd service, a
Docker container stuck in a restart loop, the OOM killer, a full disk — can only be seen
from the server. `vitalwatch-agent` runs as a service on the server, watches for these
and sends them as **error events** to the hub's server endpoint. It is language-agnostic:
.NET, Go or a static site — whatever runs on the server, its web server, database and
service manager are monitored.

It ships in the same package, with no dependencies. Recommended: a separate hub project
for each server (e.g. `vps-example`), declared as `agent`.

### Installation

```bash
sudo python3 -m venv /opt/vitalwatch
sudo /opt/vitalwatch/bin/pip install vitalwatch
sudo ln -s /opt/vitalwatch/bin/vitalwatch-agent /usr/local/bin/vitalwatch-agent

sudo install -d -m 700 /etc/vitalwatch
sudo nano /etc/vitalwatch/agent.ini          # see the example below
sudo chmod 600 /etc/vitalwatch/agent.ini

sudo vitalwatch-agent check                  # sources and configuration
sudo vitalwatch-agent check --test           # sends a test event to the dashboard
sudo sh -c 'vitalwatch-agent systemd > /etc/systemd/system/vitalwatch-agent.service'
sudo systemctl daemon-reload && sudo systemctl enable --now vitalwatch-agent
```

The virtualenv must live under `/opt`: `ProtectHome=yes` hides home directories and
`PrivateTmp=yes` hides `/tmp` from the service. The `systemd` command writes the
interpreter path of the running installation; if that path is in a home directory or
under `/tmp`, it relaxes the corresponding protection and notes it in a comment.

### `/etc/vitalwatch/agent.ini`

```ini
[hub]
url = https://vitalwatch.example.com
key = vps-key
secret = <64 characters from the dashboard>
env = production

[sources]
# all default to "auto": enabled if present and readable. "on": check fails if missing.
nginx = auto
apache = auto
plesk = auto
php_fpm = auto
postgresql = auto
mysql = auto
mongodb = auto
redis = auto
journal = auto
systemd = auto
docker = auto
disk = auto
journal_errors = off      ; all err-level journald lines (noisy)
docker_logs =             ; container names whose logs to follow, comma-separated

[disk]
threshold = 90

[ignore]
hosts =                   ; the hub's own host is always added
patterns =                ; one regular expression per line; matching raw lines are dropped

[log:plesk-panel]
path = /var/log/plesk/panel.log
pattern = \b(ERR|CRIT|ALERT|EMERG)\b
kind = log_error
```

Lookup order: `--config` → `VITALWATCH_AGENT_CONFIG` → `/etc/vitalwatch/agent.ini`.
The `VITALWATCH_URL`, `VITALWATCH_KEY`, `VITALWATCH_SECRET`, `VITALWATCH_ENV` and
`VITALWATCH_ENABLED` environment variables override `[hub]`. The agent does **not** read
`.env` files, so running `check` inside a project directory does not pick up that
project's key. Inline comments use `;` only (regular expressions may contain `#`). The
state file is `/var/lib/vitalwatch-agent/state.json` (change it with
`VITALWATCH_AGENT_STATE_DIR`).

### Commands

| Command | What it does |
|---|---|
| `vitalwatch-agent run` | Runs as a service. On SIGTERM it stops child processes, flushes the queue one last time and saves state |
| `vitalwatch-agent check` | Lists each source as `found` / `not found` / `not readable` / `disabled`; validates the hub configuration (64-character secret, https, environment name) |
| `vitalwatch-agent check --test` | Additionally sends a `log_error` test event and prints the HTTP status as is |
| `vitalwatch-agent status` | Collects one server status snapshot and prints it as JSON; nothing is sent |
| `vitalwatch-agent systemd` | Prints a hardened unit file: `root`, `ProtectSystem=strict`, `ReadWritePaths=/var/lib/vitalwatch-agent`, `NoNewPrivileges`, `ProtectHome`, `PrivateTmp`, `Restart=always` |

The service runs as root because Plesk vhost logs and the Docker socket cannot be read
otherwise. The agent writes nowhere except its state file; it runs `journalctl`,
`systemctl --failed`, `docker events`, `docker logs`, `docker ps`/`docker inspect` and
`apt-check` (or `apt-get -s upgrade`) only to **read** from them. If
`journalctl` or `docker` is missing or not permitted, it prints a single warning to
stderr and keeps running.

### What is collected

| Type | Source | Example |
|---|---|---|
| `web_server` | `/var/log/nginx/*error*.log`, `/var/log/apache2/*error*.log`, `/var/log/httpd/*error*`, Plesk `/var/www/vhosts/system/*/logs/{error_log,proxy_error_log}` | `[example.com] upstream timed out … while reading response header from upstream` (+ `route: GET /api/x`, `status: 504`) |
| `php_fpm` | `/var/log/php*-fpm.log`, `/var/log/php-fpm/*.log`, `/opt/plesk/php/*/var/log/php-fpm.log`, `/var/log/plesk-php*-fpm/error.log` | `[pool www] server reached pm.max_children setting (5), consider raising it` |
| `database` | `/var/log/postgresql/*.log`, `/var/log/mysql/error.log`, `/var/log/mariadb/*.log`, `/var/log/mysqld.log`, `/var/log/mongodb/mongod.log`, `/var/log/redis/*.log` | `FATAL: remaining connection slots are reserved …` |
| `service_failed` | journald + `systemctl --failed` every 5 minutes | `php8.4-fpm.service: Failed with result 'exit-code'` |
| `container_failed` | `docker events` | `api: exited with code 137`, `api: restart loop (3 in 10 min)`, `api: unhealthy`, `api: killed by OOM` |
| `oom` | journald (kernel, systemd-oomd) | `Out of memory: Killed process <n> (php-fpm8.4)` |
| `kernel` | journald (kernel) | `php-fpm8.4[<n>]: segfault in libc.so.6`, disk I/O errors |
| `disk_full` | Real filesystems from `/proc/mounts` | `/var: disk 93% full`, `/: inodes 91% used` — at most one per mount point per hour |
| `log_error` | `[log:<name>]` sections, `docker_logs`, `journal_errors = on` | the line matching the pattern |

- Each log is read **from the end** the first time it is seen; history is not imported.
  Positions (inode + offset) and the journald cursor are kept in the state file; log
  rotation and truncation are detected. Globs are rescanned every minute, so a new site
  added in Plesk is picked up without a restart.
- Noise is filtered out: `open() … failed` from 404s, TLS handshake errors, `[warn]`,
  PHP warnings (only `Fatal error` / `Parse error` get through), application-level SQL
  errors (that's the SDK's job), exit codes produced by `docker stop`.
- At most 20 events per minute for the same message and 300 events per minute in total.
  Events are sent every 5 s or every 25 events; bodies are split at 8 KB and 8 events per
  request. If the hub is unreachable, up to 1000 events are buffered and retried with
  exponential backoff.
- A heartbeat every 5 minutes (`{"events": [], "runtime": "agent", …}`): if the agent
  stops, the project's server channel goes quiet and the hub's setup health shows it.
- The hub's own domain (the host of `url`) is always ignored: if the hub runs on the same
  server, its own errors must not feed the agent, and the agent's sends must not trigger
  new errors.

#### Status snapshot (every 5 minutes)

Besides events, the agent sends one **server status snapshot** every 5 minutes (the first
one right after start) for the hub's daily digest and `/server` report. The hub keeps only
the latest one: it is a single snapshot, **not** a time series, and no graphs are drawn
from it. A successful snapshot also counts as the heartbeat.

| Field | Content |
|---|---|
| `hostname` | Short host name (up to the first dot) |
| `uptime_s`, `load`, `cpus` | Uptime in seconds, 1/5/15-minute load average, CPU count |
| `memory` | `total_mb`, `available_mb`, `swap_total_mb`, `swap_used_mb` |
| `disks` | Per real filesystem (same filter as `disk_full`, max 10): `mount`, `used_pct`, `inodes_pct`, `free_gb` |
| `failed_units` | `systemctl --failed` unit names (max 20); `null` without systemd |
| `containers` | `docker ps -a` + `docker inspect` (max 30): `name`, `state`, `health`, `restarts`, `started_at`; `null` if Docker is missing, not permitted or `docker = off` |
| `updates` | Debian/Ubuntu only: `pending`, `security`, `reboot_required`; counted with `apt-check` (or `apt-get -s upgrade`) at most once an hour; `null` elsewhere |

A field that cannot be collected becomes `null`; the rest is still sent. If the body
would exceed 7000 bytes, containers, then failed units, then disks are trimmed from the
end and `"truncated": true` is added. Run `vitalwatch-agent status` to see exactly what
is sent.

### Deliberately not collected

- **IP addresses** — become `[ip]` on every line; nginx's `client:` field is removed
  entirely.
- **Query strings and fragments** — stripped from request lines, URLs and paths;
  `route` carries only `GET /path`.
- **`referrer` / `referer` and `host` headers** — removed entirely. The site name comes
  from the configured `server_name` or the Plesk path, not from the client-supplied
  `host`.
- Then the SDK's masking: e-mail, card numbers, phone numbers, Turkish national ID
  (TCKN), IBAN, long tokens; quoted values in database messages become `?`.
- **SSH / authentication logs** (every line carries an IP and is meaningless without it),
  **access logs** (`access.log` — traffic, not errors; there is already a visits module)
  and **mail logs** (recipient addresses) are never read. Even with
  `journal_errors = on`, sources such as `sshd`, `sudo`, `postfix` and `dovecot` are
  skipped.
- No CPU/RAM/disk **graphs**, continuous metrics or process lists: a single event when a
  threshold is crossed, plus the latest status snapshot (above) — the hub keeps no history
  of it.

---

## Behavioral guarantees

1. **Errors are never swallowed.** The handler chain is preserved; `raise` is never
   blocked.
2. **No interference with process management.** `sys.exit` is never called; any existing
   `excepthook` is chained.
3. **It never reports its own errors.** All SDK code is wrapped in `try/except`.
4. **It never delays user requests.** Sending happens on a background thread with a
   short timeout.
5. **If the hub is unreachable, it silently gives up.**
6. **No data is sent while `VITALWATCH_ENABLED=false`.**

---

## Updating and versioning

The package is on PyPI: `pip install -U vitalwatch` installs the latest stable release.
If the version is **not pinned**, you always get the latest release; the trade-off is one
mandatory rule — **every release must bump the version number.** pip will not reinstall
a package whose version matches the installed one, and PyPI rejects a second upload with
the same number anyway. A fix shipped without a version bump **never reaches** installed
projects, and it happens silently — exactly the failure mode the whole package is
designed against.

Release steps:

1. Bump `version` in `pyproject.toml` **and** `SDK_VERSION` in
   `src/vitalwatch/reporter.py` together. If they diverge, the dashboard shows the wrong
   SDK version.
2. Add an entry to `CHANGELOG.md`.
3. `python -m pytest -q`
4. Commit, `git tag vX.Y.Z`, `git push origin vX.Y.Z`. The tag triggers the `publish`
   workflow: if the tests pass and the tag matches the package version, it is uploaded to
   PyPI.
5. In installed projects, run `pip install -U vitalwatch`, then verify with
   `vitalwatch-status`. The dashboard should show the updated SDK version. For the server
   agent: `/opt/vitalwatch/bin/pip install -U vitalwatch && systemctl restart vitalwatch-agent`.

## Tests

```bash
# For the framework tests — without these, 53 tests are silently skipped
pip install "Flask==3.0.0" Django fastapi starlette httpx
python -m unittest discover -s tests
```

If you see `skipped=` in the output, a framework is not installed and that integration
is **untested** (Flask 17, Django 12, FastAPI/Starlette 24 tests). That is exactly why
the double recording and the lost errors in 0.1.0 went unnoticed.

The tests run against a fake hub that replicates the hub's signature verification
(`VerifyProjectSignature`) and body limits exactly: this is where you see whether the
signature is really accepted — "I got a 204" is not enough.
