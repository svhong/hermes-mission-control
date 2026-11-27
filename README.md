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

Nothing to install beyond `python3`.

## What it shows

**Overview**: gateway status and uptime; active conversations, working
subagents and queued requests; today's spend, messages and tool calls; spend
and sessions per day (14 days); tokens and cost per model; conversations per
platform; tool calls with failure rates; warnings (gateway down, failed
deliveries, compression failures, repeated chat failures, handoff errors);
recently active conversations.

**Agents**: profiles with their gateway status; a tree of conversations and
the subagents they delegated to (nested subagents included, running ones
first, click a row for details); background delegations with their task and
delivery state; resumable subagents.

### Privacy

The collector reads counts, times, token figures, costs, model and tool
names. With the default settings it also sends short **titles**, the
**current activity** line, and the **goal** of background delegations
(each cut to a sentence or two). It **never** reads message text, system
prompts, reasoning, chat or user IDs, or delivery content. A test checks
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

The tab appears automatically. Ideas: scheduled jobs (cron), skills,
memory, hosted rooms (multi-agent), a log tail.

## More agents later

Every report is tagged with a source name (`HMC_SOURCE`). Another Hermes
install, on this machine or another, can run its own collector pointed at
this hub (`HMC_HUB_URL`, `HMC_TOKEN`), and a source picker appears in the
header. Profiles inside one Hermes already show up on the Agents tab.

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
