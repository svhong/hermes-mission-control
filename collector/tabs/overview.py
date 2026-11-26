"""Overview tab: what Hermes is doing now, what it has cost, and what needs attention."""
import time

from tabs import section

TAB = {"id": "overview", "title": "Overview", "order": 10}
INTERVAL = 10

DAYS = 14             # length of the daily charts
WINDOW_DAYS = 7       # window for the model / platform / tool breakdowns
ACTIVE_S = 600        # a conversation counts as active if it did something in the last 10 min
SUB_ACTIVE_S = 120    # same for subagents, which work in short bursts
HEARTBEAT_STALE_S = 120


def collect(db, ctx):
    return {
        "gateways": section(gateways, db, ctx),
        "now": section(now, db, ctx),
        "today": section(today, db, ctx),
        "daily": section(daily, db, ctx),
        "models": section(models, db, ctx),
        "platforms": section(platforms, db, ctx),
        "tools": section(tools, db, ctx),
        "warnings": section(warnings, db, ctx),
        "recent": section(recent, db, ctx),
    }


# ---- shared SQL bits ----------------------------------------------------------

def cost_sql(db, a="s"):
    actual = db.col("sessions", "actual_cost_usd", a)
    est = db.col("sessions", "estimated_cost_usd", a)
    return "COALESCE(NULLIF(%s, 0), %s, 0)" % (actual, est)


def activity_sql(db, a="s"):
    return "COALESCE(%s, %s.started_at)" % (db.col("sessions", "last_activity_at", a), a)


def tokens_sql(db, a="s"):
    return " + ".join("COALESCE(%s, 0)" % db.col("sessions", c, a, "0")
                      for c in ("input_tokens", "output_tokens"))


def window_start(ctx, days):
    lt = time.localtime(ctx.day_start)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - (days - 1), 0, 0, 0, 0, 0, -1))


# ---- sections -----------------------------------------------------------------

def gateways(db, ctx):
    if not db.has("gateway_heartbeats", "pid", "started_at", "last_heartbeat"):
        return []
    rows = db.rows("SELECT pid, started_at, last_heartbeat, %s AS profile, %s AS host "
                   "FROM gateway_heartbeats ORDER BY last_heartbeat DESC LIMIT 20"
                   % (db.col("gateway_heartbeats", "profile", default="''"),
                      db.col("gateway_heartbeats", "host", default="''")))
    for r in rows:
        r["profile"] = r["profile"] or "default"
        r["heartbeat_age_s"] = round(ctx.now - r["last_heartbeat"], 1)
        r["alive"] = r["heartbeat_age_s"] < HEARTBEAT_STALE_S
        r["uptime_s"] = round(ctx.now - r["started_at"]) if r["alive"] else None
    return rows


def now(db, ctx):
    sub = db.is_subagent_sql()
    act = activity_sql(db)
    out = {
        "active_conversations": db.one(
            "SELECT count(*) FROM sessions s WHERE s.ended_at IS NULL AND NOT %s AND %s AND %s >= ?"
            % (sub, db.visible_sql(), act), (ctx.now - ACTIVE_S,)),
        "active_subagents": db.one(
            "SELECT count(*) FROM sessions s WHERE s.ended_at IS NULL AND %s AND %s >= ?"
            % (sub, act), (ctx.now - SUB_ACTIVE_S,)),
        "workers": {},
        "queued": None,
    }
    if db.has("worker_executions", "kind", "status"):
        for r in db.rows("SELECT kind, count(*) AS n FROM worker_executions "
                         "WHERE status IN ('registered', 'running') GROUP BY kind"):
            out["workers"][r["kind"]] = r["n"]
        # A running child worker is a subagent even if it's been quiet for a bit.
        out["active_subagents"] = max(out["active_subagents"] or 0, out["workers"].get("child", 0))
    if db.has("session_admissions", "status"):
        out["queued"] = db.one("SELECT count(*) FROM session_admissions WHERE status = 'queued'")
    return out


def today(db, ctx):
    sub = db.is_subagent_sql()
    row = db.rows(
        "SELECT sum(CASE WHEN NOT %s THEN 1 ELSE 0 END) AS conversations, "
        "       sum(CASE WHEN %s THEN 1 ELSE 0 END) AS subagents, "
        "       sum(%s) AS cost, sum(%s) AS tokens "
        "FROM sessions s WHERE s.started_at >= ?"
        % (sub, sub, cost_sql(db), tokens_sql(db)), (ctx.day_start,))[0]
    out = {k: row[k] or 0 for k in row}
    # Messages and tool calls made today, including in conversations started earlier.
    # Joining through recently active sessions keeps this on the indexes.
    out["messages"] = out["tool_calls"] = 0
    if db.has("messages", "session_id", "role", "timestamp"):
        for r in db.rows(
                "SELECT m.role, count(*) AS n FROM sessions s "
                "JOIN messages m ON m.session_id = s.id AND m.timestamp >= ? "
                "WHERE %s >= ? GROUP BY m.role" % activity_sql(db), (ctx.day_start, ctx.day_start)):
            if r["role"] == "tool":
                out["tool_calls"] += r["n"]
            elif r["role"] in ("user", "assistant"):
                out["messages"] += r["n"]
    return out


def daily(db, ctx):
    """Conversations, subagents, cost and tokens per day, by the day a session started."""
    start = window_start(ctx, DAYS)
    days = []
    index = {}
    for i in range(DAYS):
        lt = time.localtime(start)
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + i, 12, 0, 0, 0, 0, -1))
        key = time.strftime("%Y-%m-%d", time.localtime(t))
        index[key] = len(days)
        days.append({"day": key, "conversations": 0, "subagents": 0, "cost": 0.0, "tokens": 0})
    for r in db.rows("SELECT s.started_at, %s AS is_sub, %s AS cost, %s AS tokens "
                     "FROM sessions s WHERE s.started_at >= ?"
                     % (db.is_subagent_sql(), cost_sql(db), tokens_sql(db)), (start,)):
        i = index.get(time.strftime("%Y-%m-%d", time.localtime(r["started_at"])))
        if i is None:
            continue
        d = days[i]
        d["subagents" if r["is_sub"] else "conversations"] += 1
        d["cost"] += r["cost"] or 0
        d["tokens"] += r["tokens"] or 0
    for d in days:
        d["cost"] = round(d["cost"], 4)
    return days


def models(db, ctx):
    """Tokens and cost per model over the window, subagents included."""
    since = window_start(ctx, WINDOW_DAYS)
    if db.has("session_model_usage", "session_id", "model"):
        u = lambda c: "sum(COALESCE(%s, 0))" % db.col("session_model_usage", c, "u", "0")  # noqa: E731
        rows = db.rows(
            "SELECT u.model AS model, %s AS input, %s AS output, %s AS cache_read, %s AS reasoning, "
            "       sum(COALESCE(NULLIF(%s, 0), %s, 0)) AS cost, %s AS api_calls "
            "FROM session_model_usage u JOIN sessions s ON s.id = u.session_id "
            "WHERE s.started_at >= ? GROUP BY u.model"
            % (u("input_tokens"), u("output_tokens"), u("cache_read_tokens"), u("reasoning_tokens"),
               db.col("session_model_usage", "actual_cost_usd", "u", "0"),
               db.col("session_model_usage", "estimated_cost_usd", "u", "0"),
               u("api_call_count")), (since,))
    else:
        rows = db.rows(
            "SELECT COALESCE(s.model, 'unknown') AS model, sum(COALESCE(%s, 0)) AS input, "
            "       sum(COALESCE(%s, 0)) AS output, 0 AS cache_read, 0 AS reasoning, "
            "       sum(%s) AS cost, sum(COALESCE(%s, 0)) AS api_calls "
            "FROM sessions s WHERE s.started_at >= ? GROUP BY 1"
            % (db.col("sessions", "input_tokens", "s", "0"), db.col("sessions", "output_tokens", "s", "0"),
               cost_sql(db), db.col("sessions", "api_call_count", "s", "0")), (since,))
    for r in rows:
        r["tokens"] = (r["input"] or 0) + (r["output"] or 0)
        r["cost"] = round(r["cost"] or 0, 4)
    rows.sort(key=lambda r: r["tokens"], reverse=True)
    return {"days": WINDOW_DAYS, "rows": rows[:10]}


def platforms(db, ctx):
    since = window_start(ctx, WINDOW_DAYS)
    rows = db.rows(
        "SELECT s.source AS platform, count(*) AS conversations, sum(%s) AS cost "
        "FROM sessions s WHERE s.started_at >= ? AND NOT %s GROUP BY s.source ORDER BY 2 DESC"
        % (cost_sql(db), db.is_subagent_sql()), (since,))
    for r in rows:
        r["cost"] = round(r["cost"] or 0, 4)
    return {"days": WINDOW_DAYS, "rows": rows}


def tools(db, ctx):
    """Tool results per tool, split by effect_disposition (success/failure labels as Hermes writes them)."""
    if not db.has("messages", "tool_name", "role", "timestamp"):
        return {"days": WINDOW_DAYS, "rows": []}
    since = window_start(ctx, WINDOW_DAYS)
    disp = db.col("messages", "effect_disposition", "m", "NULL")
    by_tool = {}
    for r in db.rows(
            "SELECT m.tool_name AS tool, COALESCE(%s, '') AS disposition, count(*) AS n "
            "FROM sessions s JOIN messages m ON m.session_id = s.id AND m.timestamp >= ? "
            "WHERE %s >= ? AND m.role = 'tool' AND m.tool_name IS NOT NULL "
            "GROUP BY 1, 2" % (disp, activity_sql(db)), (since, since)):
        t = by_tool.setdefault(r["tool"], {"tool": r["tool"], "calls": 0, "by_disposition": {}})
        t["calls"] += r["n"]
        t["by_disposition"][r["disposition"] or "unset"] = r["n"]
    rows = sorted(by_tool.values(), key=lambda t: t["calls"], reverse=True)
    return {"days": WINDOW_DAYS, "rows": rows[:12]}


def warnings(db, ctx):
    """Things worth a look, each {level: warning|critical, text}."""
    out = []
    gws = gateways(db, ctx) if db.has("gateway_heartbeats", "last_heartbeat") else []
    if db.has("gateway_heartbeats", "last_heartbeat") and not any(g["alive"] for g in gws):
        out.append({"level": "critical", "text": "No gateway heartbeat in the last %d s" % HEARTBEAT_STALE_S})

    if db.has("delivery_obligations", "state", "updated_at"):
        # Never read the content column: it's the message text.
        err = db.col("delivery_obligations", "last_error", default="NULL")
        n = db.one("SELECT count(*) FROM delivery_obligations WHERE %s IS NOT NULL AND updated_at >= ? "
                   "AND state NOT IN ('delivered', 'done', 'complete', 'completed', 'succeeded', 'sent')"
                   % err, (ctx.now - 86400,))
        if n:
            out.append({"level": "warning", "text": "%d outgoing message(s) failed to deliver in the last 24 h" % n})

    if db.has("sessions", "compression_failure_cooldown_until"):
        n = db.one("SELECT count(*) FROM sessions WHERE ended_at IS NULL AND compression_failure_cooldown_until > ?",
                   (ctx.now,))
        if n:
            out.append({"level": "warning", "text": "%d conversation(s) waiting after a failed context compression" % n})

    if db.has("gateway_hygiene_state", "failure_streak"):
        row = db.rows("SELECT count(*) AS n, max(failure_streak) AS worst FROM gateway_hygiene_state "
                      "WHERE failure_streak > 0")[0]
        if row["n"]:
            out.append({"level": "critical" if row["worst"] >= 5 else "warning",
                        "text": "%d chat(s) with repeated gateway failures (worst streak %d)" % (row["n"], row["worst"])})

    if db.has("sessions", "handoff_error"):
        n = db.one("SELECT count(*) FROM sessions s WHERE handoff_error IS NOT NULL AND %s >= ?"
                   % activity_sql(db), (ctx.now - 86400,))
        if n:
            out.append({"level": "warning", "text": "%d platform handoff error(s) in the last 24 h" % n})
    return out


def recent(db, ctx):
    """Most recently active conversations (not subagents)."""
    sub = db.is_subagent_sql()
    rows = db.rows(
        "SELECT s.id, s.source, %s AS profile, s.model, s.started_at, s.ended_at, %s AS end_reason, "
        "       %s AS last_activity_at, %s AS title, %s AS activity, "
        "       COALESCE(%s, 0) AS messages, COALESCE(%s, 0) AS tool_calls, %s AS tokens, %s AS cost, "
        "       (SELECT count(*) FROM sessions c WHERE c.started_at >= s.started_at "
        "          AND (c.parent_session_id = s.id OR %s = s.id) AND %s) AS subagents "
        "FROM sessions s WHERE NOT %s AND %s ORDER BY %s DESC LIMIT 12"
        % (db.col("sessions", "profile_name", "s"), db.col("sessions", "end_reason", "s"),
           activity_sql(db), db.col("sessions", "title", "s"),
           db.col("sessions", "last_activity_description", "s"),
           db.col("sessions", "message_count", "s", "0"), db.col("sessions", "tool_call_count", "s", "0"),
           tokens_sql(db), cost_sql(db), db.delegate_from_sql("c"), db.is_subagent_sql("c"),
           sub, db.visible_sql(), activity_sql(db)))
    for r in rows:
        r["profile"] = r["profile"] or "default"
        r["title"] = ctx.text(r["title"], 80)
        r["activity"] = ctx.text(r["activity"], 120)
        r["cost"] = round(r["cost"] or 0, 4)
        r["active"] = r["ended_at"] is None and ctx.now - (r["last_activity_at"] or 0) < ACTIVE_S
    return rows
