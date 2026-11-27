"""Scheduled tab: Hermes's cron jobs, their recent runs, and anything that went wrong.

Job definitions come from <hermes home>/cron/jobs.json (next to state.db);
runs are cron sessions in state.db, titled "<job name> · Nov 26 08:05".

Never sent: job prompts, the chat id in "deliver" (only the platform),
"origin", scripts, workdirs or run output.
"""
import json
import os
import re
import time
from datetime import datetime

from tabs import section
from tabs.overview import ACTIVE_S, activity_sql, current_turn, tokens_sql

TAB = {"id": "scheduled", "title": "Scheduled", "order": 15}
INTERVAL = 15

RUN_WINDOW_DAYS = 30
RUNS_PER_JOB = 14
OVERDUE_GRACE_S = 600       # a job is overdue if its next run is this far in the past
TICKER_STALE_S = 300        # the scheduler counts as stalled after this long without a tick
FAILED = re.compile(r"error|fail|crash|abort|timeout|timed.out|exception|cancel", re.I)
DAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def collect(db, ctx):
    cron_dir = os.path.join(os.path.dirname(os.path.abspath(db.path)), "cron")
    jobs_data = section(jobs, db, ctx, cron_dir)
    return {
        "scheduler": section(scheduler, cron_dir, ctx),
        "jobs": jobs_data,
        "running": section(running, db, ctx),
        "today": section(today, db, ctx),
        "problems": section(problems, jobs_data, ctx),
    }


# ---- helpers ------------------------------------------------------------------------

def to_epoch(value):
    """ISO time (with offset), or epoch seconds, to epoch seconds. None if unknown."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _days(field):
    """Day-of-week field ("1,4", "1-5", "*") -> sorted day numbers (0 = Sun), or None."""
    if field == "*":
        return None
    out = set()
    for part in field.split(","):
        if re.fullmatch(r"\d+-\d+", part):
            a, b = (int(x) for x in part.split("-"))
            out.update(range(a, b + 1))
        elif part.isdigit():
            out.add(int(part))
        else:
            raise ValueError(part)
    return sorted({d % 7 for d in out})


def describe(expr):
    """A cron expression in plain words, e.g. "0 9 * * 1,4" -> "Mon & Thu 09:00".
    Falls back to the expression itself for anything unusual."""
    parts = str(expr or "").split()
    if len(parts) != 5:
        return str(expr or "")
    minute, hour, dom, month, dow = parts
    try:
        if minute.isdigit() and hour == "*" and dom == month == dow == "*":
            return "Hourly at :%02d" % int(minute)
        if minute.startswith("*/") and hour == "*" and dom == month == dow == "*":
            return "Every %s min" % minute[2:]
        if minute.isdigit() and hour.startswith("*/") and dom == month == dow == "*":
            return "Every %s h at :%02d" % (hour[2:], int(minute))
        if not (minute.isdigit() and hour.isdigit()):
            return expr
        at = "%02d:%02d" % (int(hour), int(minute))
        if month != "*":
            return expr
        if dom == "*":
            days = _days(dow)
            if days is None or len(days) == 7:
                return "Daily " + at
            if days == [1, 2, 3, 4, 5]:
                return "Weekdays " + at
            if days == [0, 6]:
                return "Weekends " + at
            names = [DAY_NAMES[d] for d in days]
            return "%s %s" % (" & ".join(names) if len(names) <= 2 else ", ".join(names), at)
        if dom.isdigit() and dow == "*":
            return "Monthly on day %s %s" % (dom, at)
    except ValueError:
        pass
    return expr


def run_name(title):
    """Job name from a run's title: "Israel morning briefing · Nov 26 08:05" -> "Israel morning briefing"."""
    return re.split(r"\s+·\s+", title or "", maxsplit=1)[0].strip()


def run_status(row, ctx):
    if row["ended_at"] is None:
        quiet = ctx.now - (row["last_activity_at"] or row["started_at"])
        return "running" if quiet < ACTIVE_S else "unknown"
    reason = row["end_reason"] or ""
    if FAILED.search(reason):
        return "failed"
    return "ok"


def load_jobs(cron_dir):
    path = os.path.join(cron_dir, "jobs.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    items = data.get("jobs", data) if isinstance(data, dict) else data
    if isinstance(items, dict):
        items = list(items.values())
    return [j for j in items if isinstance(j, dict)]


# ---- sections -----------------------------------------------------------------------

def scheduler(cron_dir, ctx):
    """Is Hermes's scheduler ticking? From cron/ticker_heartbeat (content or file time)."""
    out = {}
    for key, name in (("heartbeat_at", "ticker_heartbeat"), ("last_success_at", "ticker_last_success")):
        path = os.path.join(cron_dir, name)
        try:
            with open(path) as f:
                t = to_epoch(f.read().strip())
            out[key] = t if t else os.path.getmtime(path)
        except OSError:
            out[key] = None
    if out["heartbeat_at"] is None:
        out["ticking"] = None  # this Hermes doesn't have the file: unknown
    else:
        out["age_s"] = round(ctx.now - out["heartbeat_at"], 1)
        out["ticking"] = out["age_s"] < TICKER_STALE_S
    return out


def jobs(db, ctx, cron_dir):
    defs = load_jobs(cron_dir)

    # Runs: cron sessions from the last few weeks, grouped by job name.
    runs_by_name = {}
    if db.has("sessions", "source", "started_at"):
        c = lambda col, default="NULL": db.col("sessions", col, "s", default)  # noqa: E731
        for r in db.rows(
                "SELECT s.id, %s AS title, s.started_at, s.ended_at, %s AS end_reason, %s AS last_activity_at, "
                "       %s AS tokens, COALESCE(%s, 0) AS tool_calls "
                "FROM sessions s WHERE s.source = 'cron' AND s.started_at >= ? ORDER BY s.started_at"
                % (c("title"), c("end_reason"), activity_sql(db), tokens_sql(db), c("tool_call_count", "0")),
                (ctx.now - RUN_WINDOW_DAYS * 86400,)):
            r["status"] = run_status(r, ctx)
            r["duration_s"] = round((r["ended_at"] or ctx.now) - r["started_at"])
            runs_by_name.setdefault(run_name(r.pop("title")), []).append(r)

    out = []
    for j in defs or []:
        name = str(j.get("name") or j.get("id") or "")
        sched = j.get("schedule") if isinstance(j.get("schedule"), dict) else {}
        expr = sched.get("expr") or sched.get("display") or j.get("schedule_display") or ""
        repeat = j.get("repeat") if isinstance(j.get("repeat"), dict) else {}
        dispatch = j.get("last_dispatch") if isinstance(j.get("last_dispatch"), dict) else {}
        runs = runs_by_name.get(name, [])
        enabled = bool(j.get("enabled", True)) and not j.get("paused_at")
        next_at = to_epoch(j.get("next_run_at"))
        job = {
            "id": str(j.get("id") or ""),
            "name": ctx.text(name, 80),
            "schedule": describe(expr) if sched.get("kind", "cron") == "cron" else (sched.get("display") or expr),
            "expr": expr,
            "timezone": j.get("timezone"),
            "enabled": enabled,
            "state": j.get("state"),
            "paused_at": to_epoch(j.get("paused_at")),
            "paused_reason": ctx.text(j.get("paused_reason"), 120),
            "next_run_at": next_at if enabled else None,
            "last_run_at": to_epoch(j.get("last_run_at")),
            "last_status": j.get("last_status"),
            "failure_streak": int(j.get("failure_streak") or 0),
            "last_error": ctx.text(j.get("last_error"), 160),
            "last_delivery_error": ctx.text(j.get("last_delivery_error"), 160),
            "repeat_times": repeat.get("times"),
            "repeat_completed": repeat.get("completed"),
            # "telegram:<chat id>" -> "telegram": the chat id is private.
            "deliver": str(j.get("deliver")).split(":", 1)[0] if j.get("deliver") else None,
            "skills": len(j["skills"]) if isinstance(j.get("skills"), list) else 0,
            "model": j.get("model"),
            "lateness_s": dispatch.get("lateness_seconds"),
            "dispatch_kind": dispatch.get("kind"),
            "runs": runs[-RUNS_PER_JOB:],
            "runs_total": len(runs),
            "overdue": bool(enabled and next_at and ctx.now - next_at > OVERDUE_GRACE_S),
        }
        job["status"] = job_status(job)
        out.append(job)
    order = {"running": 0, "failing": 1, "overdue": 2, "ok": 3, "paused": 4, "done": 5}
    out.sort(key=lambda x: (order.get(x["status"], 5), x["next_run_at"] or float("inf")))
    matched = {str(j.get("name") or "") for j in defs or []}
    return {
        "jobs_file": defs is not None,
        "jobs": out,
        # Runs whose job isn't in jobs.json any more (deleted or renamed jobs).
        "other_runs": sum(len(v) for k, v in runs_by_name.items() if k not in matched),
    }


def job_status(job):
    if job["runs"] and job["runs"][-1]["status"] == "running":
        return "running"
    if str(job["state"] or "").lower() in ("completed", "complete", "done", "finished", "exhausted"):
        return "done"   # e.g. a job limited to N runs that has done them all
    if not job["enabled"]:
        return "paused"
    if job["failure_streak"] > 0 or (job["last_status"] not in (None, "ok", "success", "succeeded")):
        return "failing"
    if job["overdue"]:
        return "overdue"
    return "ok"


def running(db, ctx):
    """Cron runs in progress, with what the run has done so far."""
    rows = db.rows(
        "SELECT s.id, %s AS title, s.started_at, %s AS last_activity_at, %s AS activity "
        "FROM sessions s WHERE s.source = 'cron' AND s.ended_at IS NULL AND %s >= ? ORDER BY s.started_at"
        % (db.col("sessions", "title", "s"), activity_sql(db), db.col("sessions", "last_activity_description", "s"),
           activity_sql(db)), (ctx.now - ACTIVE_S,))
    for r in rows:
        r["name"] = ctx.text(run_name(r.pop("title")), 80)
        r["activity"] = ctx.text(r["activity"], 120)
        r["turn"] = current_turn(db, r["id"])
    return rows


def today(db, ctx):
    out = {"ok": 0, "failed": 0, "running": 0}
    for r in db.rows("SELECT s.ended_at, %s AS end_reason, %s AS last_activity_at, s.started_at "
                     "FROM sessions s WHERE s.source = 'cron' AND s.started_at >= ?"
                     % (db.col("sessions", "end_reason", "s"), activity_sql(db)), (ctx.day_start,)):
        status = run_status(r, ctx)
        if status in out:
            out[status] += 1
    return out


def problems(jobs_data, ctx):
    """Things worth a look, each {level, text}."""
    if not isinstance(jobs_data, dict) or "_error" in jobs_data:
        return []
    out = []
    for j in jobs_data["jobs"]:
        name = j["name"] or j["id"]
        if j["status"] == "failing":
            text = "%s has failed %d time%s in a row" % (name, j["failure_streak"], "" if j["failure_streak"] == 1 else "s") \
                if j["failure_streak"] else "%s's last run ended with status %r" % (name, j["last_status"])
            if j["last_error"]:
                text += ": " + j["last_error"]
            out.append({"level": "critical" if j["failure_streak"] >= 3 else "warning", "text": text})
        elif j["last_delivery_error"]:
            out.append({"level": "warning", "text": "%s ran, but delivering the result failed: %s"
                        % (name, j["last_delivery_error"])})
        if j["overdue"]:
            out.append({"level": "warning", "text": "%s should have run %s and hasn't"
                        % (name, time.strftime("%a %H:%M", time.localtime(j["next_run_at"])))})
    return out
