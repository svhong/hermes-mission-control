# Hermes Mission Control

A lightweight, **read-only** dashboard for the Hermes agent. It shows what
Hermes is doing now, what it has spent, how its tools are doing, and the
subagents it has delegated work to. It works on desktop and phone and is
only reachable over Tailscale.

Machine health (CPU, temperatures, disk…) stays in
[garage-monitor](https://github.com/svhong/garage-monitor); the header links to it.

```
 hermes-box
┌──────────────────────────────────────────────────────────────────┐
│  ~hermes/.hermes/state.db                                        │
│        │ read-only                                               │
│  collector (runs as hermes, sandboxed) ──► hub (runs as hmc) ──► │ ◄── browser over Tailscale
└──────────────────────────────────────────────────────────────────┘
```

- **collector** (`collector/`): runs as the Hermes user, opens `state.db`
  read-only every few seconds and sends a small summary to the hub. It can't
  write anywhere and can only talk to localhost.
- **hub** (`hub/hub.py`): receives summaries, serves the dashboard, checks the
  viewer password. Standard library only; it never touches Hermes's files.
- **dashboard** (`hub/static/`): plain HTML, CSS and JavaScript modules. No
  npm, no build step.

Nothing to install beyond `python3`. It runs either as two systemd services
next to Hermes ([Install on hermes-box](#install-on-hermes-box)) or, together
with Hermes itself, as Docker containers ([Run in Docker](#run-in-docker)).

## What it shows

**Overview**: gateway status and uptime; active conversations, working
subagents and queued requests; today's spend, messages and tool calls; spend
and sessions per day (14 days); tokens and cost per model; conversations per
platform; tool calls with failure rates; warnings (gateway down, failed
deliveries, compression failures, repeated chat failures, handoff errors);
recently active conversations.

**Scheduled**: whether Hermes's scheduler is ticking; the next run; runs
today; every cron job with its schedule in plain words ("Mon & Thu 09:00"),
next and last run, and a strip of its recent runs (click for details such as
delivery platform, timing and the last error); jobs that are failing, overdue
or whose results couldn't be delivered; and runs in progress. Job definitions
come from `~/.hermes/cron/jobs.json`, runs from the database.

**Agents**: profiles with their gateway status; a tree of conversations and
the subagents they delegated to (nested subagents included, running ones
first, click a row for details); background delegations with their task and
delivery state; resumable subagents.

**Containers** (Docker setup): every container on the machine, grouped by
the agent it belongs to: running/healthy/unhealthy/restarting, uptime,
restarts (and restarts in the last hour), CPU and memory with a sparkline of
recent readings and their limits, network rates and process count; problems
such as failing health checks, crash loops, containers killed for memory or
stopped with an error. The header shows a summary on every tab.

### Privacy

The collector reads counts, times, token figures, costs, model and tool
names. With the default settings it also sends short **titles**, the
**current activity** line, and the **goal** of background delegations
(each cut to a sentence or two), plus job names and their last error. It
**never** reads message text, system prompts, reasoning, chat or user IDs,
delivery content, or scheduled jobs' prompts and output. A test checks
this on every run.

For numbers only, install with `--numbers-only`.

### Resource use

Lightweight by design. Measured on hermes-box: the collector used about
0.2% of one CPU core and 18 MB of memory, and each read of Hermes's database
took about 5 ms. It reads every 5 seconds only while a dashboard is open; when
nobody is watching it reads once a minute (`HMC_IDLE_INTERVAL`) and just asks
the hub every 5 seconds whether someone opened the dashboard, which doesn't
touch the database. Hidden browser tabs don't count as watching. The hub
writes one small file (the daily token ledger) once a minute.

## Install on hermes-box

```bash
git clone https://github.com/svhong/hermes-mission-control.git
cd hermes-mission-control
sudo ./install.sh --garage-url http://hermes-box:8787
```

The installer:

1. Checks for Python, Tailscale and the Hermes database (default
   `~hermes/.hermes/state.db`; change with `--hermes-user` or `--db`).
2. Creates a locked-down system user `hmc` for the hub.
3. Copies the program to `/opt/hermes-mission-control` and its settings to
   `/etc/hermes-mission-control`.
4. Asks for a viewer password.
5. Checks that the collector can read the database before going further.
6. Sets up two systemd services: the hub (listening only on the Tailscale IP,
   port **8788**) and the collector.
7. Prints the dashboard address, e.g. `http://hermes-box:8788`.

To update after a `git pull`, run `sudo ./install.sh` again. Settings are kept.

| Option | |
|---|---|
| `--set-password` | change the viewer password |
| `--numbers-only` | don't send titles, activity or task goals |
| `--garage-url URL` | "Machine health" link in the header |
| `--https` | also serve on `https://<machine>.<tailnet>.ts.net:8443` via `tailscale serve` |
| `--port N` | dashboard port (default 8788) |
| `--uninstall` | remove everything (Hermes isn't touched) |

## Run in Docker

Runs the agent, **Nova** (Hermes, from the official `nousresearch/hermes-agent`
image), and Mission Control as containers on hermes-box. Nova's data (config,
API keys, conversations, cron jobs, skills) lives in the `nova-data` volume.

```
 hermes-box (Docker)
┌──────────────────────────────────────────────────────────────────────┐
│ nova ── internet (LLM APIs, Telegram…)          volume nova-data     │
│   │ same process list                           (read-only below)    │
│ nova-collector ─┐                                                    │
│                 ├──► hmc-hub ──► 127.0.0.1:8788 ──► tailscale serve  │
│ hmc-docker ─────┘                                                    │
│   │                                                                  │
│ hmc-docker-proxy (GET containers only) ── /var/run/docker.sock (ro)  │
└──────────────────────────────────────────────────────────────────────┘
```

| Container | What it does |
|---|---|
| `nova` | Hermes's messaging gateway (`gateway run`). Docker starts it at boot and restarts it if it crashes |
| `nova-collector` | reads Nova's database read-only, as Nova's user. Shares the `nova` container's process list so it can see the gateway process; has no internet access |
| `hmc-hub` | the dashboard |
| `hmc-docker` | the container monitor (`collector/docker_monitor.py`): feeds the Containers tab |
| `hmc-docker-proxy` | [docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy): lets the monitor list and inspect containers and nothing else (GET only, no images, exec or secrets). Nothing else gets the Docker socket |

Every Mission Control container runs with a read-only filesystem, no Linux
capabilities, memory and CPU limits, and a health check, so `docker compose ps`
and the Containers tab both show whether each one is healthy. Logs rotate at
3 × 10 MB per container.

### Set up

Your account needs to be allowed to use Docker (log out and back in afterwards):

```bash
sudo usermod -aG docker "$USER"
```

Then:

```bash
sudo ./docker/setup.sh --up
```

The script:

1. Writes `.env` (mode 600; see `.env.example` for every setting), carrying
   over the token and viewer password from a systemd install of Mission Control
   if there is one, and asks for a viewer password otherwise.
2. Warns about anything that would clash: the systemd Mission Control services
   (port 8788) and a Hermes gateway running outside Docker (it would answer the
   same chats twice).
3. Puts the dashboard on your tailnet with `tailscale serve --tcp 8788`. The hub
   container only listens on `127.0.0.1`, and `http://hermes-box:8788` works
   from your other devices. Add `--https` for `https://hermes-box.<tailnet>.ts.net:8443`.
4. With `--up`: builds Mission Control, pulls Hermes, runs Hermes's setup wizard
   the first time (model, API keys, chat platforms), and starts everything.

Without `--up`, run Nova's setup yourself, then start:

```bash
docker compose run --rm nova setup
```
```bash
docker compose up -d --build
```

| Task | Command |
|---|---|
| Status and health | `docker compose ps` |
| Logs | `docker compose logs -f nova` (or `nova-collector`, `hmc-hub`, `hmc-docker`) |
| Run a Hermes command | `docker compose exec -u hermes nova hermes <command>` (e.g. `status`, `doctor`). Keep `-u hermes`: as root, Hermes would leave files Nova can't change |
| Change Nova's settings | `docker compose exec -u hermes nova hermes setup`, then `docker compose restart nova` |
| Update Mission Control | `git pull && docker compose up -d --build` |
| Update Hermes | `docker compose pull nova && docker compose up -d` (pin a version with `HERMES_IMAGE` in `.env`) |
| Back up Nova | `docker run --rm -v hermes-mission-control_nova-data:/data -v "$PWD":/backup busybox tar czf /backup/nova-backup.tgz -C /data .` |
| New viewer password | `sudo ./docker/setup.sh --set-password && docker compose up -d` |
| What the collector sends | `docker compose exec nova-collector python3 /app/collector/collector.py --once` |
| What the monitor sees | `docker compose exec hmc-docker python3 /app/collector/docker_monitor.py --once` |
| Stop everything | `docker compose down` (data stays) |

Notes:
- Nova's tools run inside its container. Anything it should use from the
  host (other programs, folders) must be mounted or added to the image. The
  Hermes image already includes git, Python, the libraries for a headless
  browser, and the Docker CLI.
- To keep Nova's data in a folder instead of a volume, set `NOVA_DATA=/some/path`
  in `.env`; the folder should belong to uid 10000 (or set `HERMES_UID`/`HERMES_GID`).
- The Containers tab shows every container on the machine. Set `HMC_DOCKER_ALL=0`
  to show only this stack's containers.
- The container monitor also works without the rest of the Docker setup:
  `HMC_DOCKER_URL=unix:///var/run/docker.sock python3 collector/docker_monitor.py`
  with the usual `HMC_HUB_URL` / `HMC_TOKEN`. That needs read access to the
  Docker socket, which is effectively root.

### Try the Docker setup with a fake Hermes

```bash
HMC_TOKEN=dev docker compose -f docker-compose.yml -f docker-compose.fake.yml up -d --build
```

Open http://127.0.0.1:8788. `docker compose -f docker-compose.yml -f docker-compose.fake.yml down -v` removes it.

## Try it on any computer

```bash
python3 tools/fake_hermes.py --db /tmp/hmc/state.db --live          # terminal 1: a fake, busy Hermes
HMC_TOKEN=dev python3 hub/hub.py                                     # terminal 2
HMC_TOKEN=dev HMC_DB=/tmp/hmc/state.db python3 collector/collector.py  # terminal 3
```

Then open http://127.0.0.1:8788. The fake database uses Hermes's real
schema with made-up data, and `--live` keeps conversations and subagents
moving so every status shows up.

See exactly what would be sent from the real database:

```bash
sudo -u hermes python3 /opt/hermes-mission-control/collector/collector.py --once
```

Run the tests with `python3 -m unittest discover -s tests`.

## Adding a tab

Each tab is two files:

- `collector/tabs/<id>.py` with `TAB = {"id": "<id>", "title": "...", "order": 30}`,
  an optional `INTERVAL` in seconds, and `collect(db, ctx)` returning a
  JSON-safe dict. Use `db.has(table, *columns)` and `db.col(...)` so the tab
  keeps working when Hermes changes its schema, and `ctx.text(...)` for any
  free text so `--numbers-only` is respected.
- `hub/static/tabs/<id>.js` exporting `render(root, ctx)`, using the helpers
  in `hub/static/lib.js` (cards, tiles, tables, bar and column charts, status
  pills).

The tab appears automatically. Ideas: skills, memory, hosted rooms
(multi-agent), a log tail.

## More agents later

Every report is tagged with a source name (`HMC_SOURCE`). Another Hermes
install, on this machine or another, can run its own collector pointed at
this hub (`HMC_HUB_URL`, `HMC_TOKEN`), and an agent picker appears in the
header. Profiles inside one Hermes already show up on the Agents tab.

With Docker, another agent is two more services in `docker-compose.yml`: copy
the `nova` and `nova-collector` blocks and rename every `nova` in them (the
comment at the top of the file lists each place), add its data volume, then
`docker compose run --rm <name> setup` and `docker compose up -d`. The
`hmc.source` label groups its containers under that agent on the Containers tab.

Sources come in kinds: `hermes` (an agent, listed in the picker) and
`docker` (the container monitor). A tab whose `TAB` has `"global": True` is
shown from whichever source reports it, whatever agent is picked.

## Troubleshooting

| What | Command |
|---|---|
| Service status | `systemctl status hermes-mission-control-hub hermes-mission-control-collector` |
| Live logs | `journalctl -u hermes-mission-control-collector -f` |
| What the collector sends | `sudo -u hermes python3 /opt/hermes-mission-control/collector/collector.py --once` |
| Remove everything | `sudo ./install.sh --uninstall` |

The footer of the dashboard shows the Hermes schema version, how long each
collection took, and "Hermes not running (snapshot)" when Hermes is stopped
and the database was read as a snapshot. A section that says "Couldn't read
this from Hermes" usually means a Hermes update changed the schema. The rest
of the dashboard keeps working; send the error and `.schema` output to get
it updated.
