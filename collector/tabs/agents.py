"""Agents tab: profiles, and a tree of conversations and the subagents they delegated to.

Shape of the tree (works the same once there are several standalone agents,
since each one is a profile and/or its own source):

    profile -> conversation -> subagent -> nested subagent ...

A conversation that Hermes continued in a new session after compressing its
context is shown once: continuation sessions are folded into the original.
"""
import json
import re

from hermes_db import json_strings, parse_list, pick_text
from tabs import section
from tabs.overview import ACTIVE_S, SUB_ACTIVE_S, activity_sql, cost_sql, current_turn, gateways

TAB = {"id": "agents", "title": "Agents", "order": 20}
INTERVAL = 5

WINDOW_S = 48 * 3600     # subagents started in this window are listed
PROFILE_WINDOW_S = 7 * 86400
MAX_SUBAGENTS = 400
MAX_CONVERSATIONS = 30
FAILED = re.compile(r"error|fail|crash|abort|timeout|timed.out|exception", re.I)
# A session that ended like this was continued in a new session (same conversation).
# Anything else with a parent, e.g. "session_reset", is a new conversation.
CONTINUED = re.compile(r"compress|compact|continu|rollover|context", re.I)
# Titles Hermes gives subagents that say nothing about the task.
GENERIC_TITLE = re.compile(r"^\s*subagent\b[:\s]*(context[:\s]*)?#?\d*\s*$", re.I)
MATCH_WINDOW_S = 10


def collect(db, ctx):
    return {
        "profiles": section(profiles, db, ctx),
        "tree": section(tree, db, ctx),
        "delegations": section(delegations, db, ctx),
        "resumable": section(resumable, db, ctx),
        "window_h": WINDOW_S // 3600,
    }


# ---- profiles ---------------------------------------------------------------------

def profiles(db, ctx):
    sub = db.is_subagent_sql()
    prof = "COALESCE(NULLIF(%s, ''), 'default')" % db.col("sessions", "profile_name", "s", "NULL")
    out = {}
    for r in db.rows(
            "SELECT %s AS profile, sum(CASE WHEN NOT %s THEN 1 ELSE 0 END) AS conversations, "
            "       sum(CASE WHEN %s THEN 1 ELSE 0 END) AS subagents, sum(%s) AS cost, max(%s) AS last_activity_at "
            "FROM sessions s WHERE s.started_at >= ? GROUP BY 1"
            % (prof, sub, sub, cost_sql(db), activity_sql(db)), (ctx.now - PROFILE_WINDOW_S,)):
        r["cost"] = round(r["cost"] or 0, 4)
        r.update(gateway_alive=None, gateway_uptime_s=None)
        out[r["profile"]] = r
    gws = gateways(db, ctx)
    for g in gws if isinstance(gws, list) else []:
        p = out.setdefault(g["profile"], {"profile": g["profile"], "conversations": 0, "subagents": 0,
                                          "cost": 0, "last_activity_at": None,
                                          "gateway_alive": None, "gateway_uptime_s": None})
        if g["alive"] or p["gateway_alive"] is None:
            p["gateway_alive"] = g["alive"]
            p["gateway_uptime_s"] = g["uptime_s"]
    return sorted(out.values(), key=lambda p: (p["profile"] != "default", p["profile"]))


# ---- conversation / subagent tree ---------------------------------------------------

def _node_sql(db):
    c = lambda col, default="NULL": db.col("sessions", col, "s", default)  # noqa: E731
    return (
        "SELECT s.id, s.source, s.parent_session_id AS parent, %s AS delegate_from, %s AS is_sub, "
        "       %s AS profile, s.model, s.started_at, s.ended_at, %s AS end_reason, %s AS last_activity_at, "
        "       %s AS title, %s AS activity, COALESCE(%s, 0) AS messages, COALESCE(%s, 0) AS tool_calls, "
        "       COALESCE(%s, 0) AS api_calls, COALESCE(%s, 0) AS tokens_in, COALESCE(%s, 0) AS tokens_out, "
        "       %s AS cost, %s AS tool_names "
        "FROM sessions s "
        % (db.delegate_from_sql(), db.is_subagent_sql(), c("profile_name"), c("end_reason"),
           activity_sql(db), c("title"), c("last_activity_description"), c("message_count", "0"),
           c("tool_call_count", "0"), c("api_call_count", "0"), c("input_tokens", "0"),
           c("output_tokens", "0"), cost_sql(db), c("tool_names"))
    )


def _delegate_parent(value):
    """_delegate_from is usually the parent session id; accept a JSON object too."""
    if not value:
        return None
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            obj = json.loads(value)
        except ValueError:
            return None
        for k in ("session_id", "parent_session_id", "parent", "id"):
            if isinstance(obj.get(k), str):
                return obj[k]
        return None
    return str(value)


def tree(db, ctx):
    base = _node_sql(db)
    sub = db.is_subagent_sql()
    nodes = {}

    def add(rows):
        for r in rows:
            r["is_sub"] = bool(r["is_sub"])
            r["parent_id"] = _delegate_parent(r.pop("delegate_from")) or r.pop("parent")
            r.pop("parent", None)
            nodes[r["id"]] = r

    add(db.rows(base + "WHERE %s AND (s.started_at >= ? OR (s.ended_at IS NULL AND %s >= ?)) "
                       "ORDER BY s.started_at DESC LIMIT %d" % (sub, activity_sql(db), MAX_SUBAGENTS),
                (ctx.now - WINDOW_S, ctx.now - WINDOW_S)))
    add(db.rows(base + "WHERE NOT %s AND %s AND s.ended_at IS NULL AND %s >= ?"
                       % (sub, db.visible_sql(), activity_sql(db)), (ctx.now - ACTIVE_S,)))

    # Load missing parents (and the sessions they continued from) a few levels up.
    gone = set()  # parents that no longer exist; don't ask for them again
    for _ in range(8):
        missing = {n["parent_id"] for n in nodes.values()
                   if n["parent_id"] and n["parent_id"] not in nodes and n["parent_id"] not in gone}
        if not missing:
            break
        ids = list(missing)[:500]
        add(db.rows(base + "WHERE s.id IN (%s)" % ",".join("?" * len(ids)), ids))
        gone.update(i for i in ids if i not in nodes)

    workers = {}
    if db.has("worker_executions", "session_id", "status") and nodes:
        ids = list(nodes)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for w in db.rows("SELECT session_id, kind, status FROM worker_executions WHERE session_id IN (%s)"
                             % ",".join("?" * len(chunk)), chunk):
                # Prefer a live status if a session has several executions.
                if w["status"] in ("running", "registered") or w["session_id"] not in workers:
                    workers[w["session_id"]] = w["status"]

    def root_of(nid):
        """Follow compression continuations back to the original conversation."""
        n = nodes[nid]
        seen = set()
        while (not n["is_sub"] and n["parent_id"] in nodes and not nodes[n["parent_id"]]["is_sub"]
               and CONTINUED.search(nodes[n["parent_id"]]["end_reason"] or "") and n["id"] not in seen):
            seen.add(n["id"])
            n = nodes[n["parent_id"]]
        return n["id"]

    # Group conversation sessions into conversations.
    convs = {}
    for nid, n in nodes.items():
        if not n["is_sub"]:
            convs.setdefault(root_of(nid), []).append(n)

    goals = subagent_goals(db, ctx, nodes)
    out_nodes = {}

    def to_out(n, kind):
        o = {
            "id": n["id"], "kind": kind, "source": n["source"], "profile": n["profile"] or "default",
            "model": n["model"], "started_at": n["started_at"], "ended_at": n["ended_at"],
            "end_reason": n["end_reason"], "last_activity_at": n["last_activity_at"],
            "title": ctx.text(n["title"], 80), "activity": ctx.text(n["activity"], 140),
            "messages": n["messages"], "tool_calls": n["tool_calls"], "api_calls": n["api_calls"],
            "tokens_in": n["tokens_in"], "tokens_out": n["tokens_out"], "cost": round(n["cost"] or 0, 4),
            "tool_names": parse_list(n["tool_names"]), "worker": workers.get(n["id"]),
            "status": _status(n, workers.get(n["id"]), ctx), "children": [],
            "goal": ctx.text(goals.get(n["id"]), 160),
        }
        if o["goal"] and (not o["title"] or GENERIC_TITLE.match(o["title"])):
            o["title"] = ctx.text(o["goal"], 80)
        if o["ended_at"]:
            o["duration_s"] = round(o["ended_at"] - o["started_at"])
        else:
            o["duration_s"] = round(ctx.now - o["started_at"])
        return o

    for cid, members in convs.items():
        members.sort(key=lambda m: m["started_at"])
        head = max(members, key=lambda m: m["last_activity_at"] or 0)
        o = to_out(head, "conversation")
        o["id"] = cid
        o["started_at"] = members[0]["started_at"]
        o["title"] = o["title"] or next((ctx.text(m["title"], 80) for m in members if m["title"]), None)
        o["continuations"] = len(members) - 1
        for k in ("messages", "tool_calls", "api_calls", "tokens_in", "tokens_out"):
            o[k] = sum(m[k] for m in members)
        o["cost"] = round(sum(m["cost"] or 0 for m in members), 4)
        o["duration_s"] = round((head["ended_at"] or ctx.now) - o["started_at"])
        o["turn"] = current_turn(db, head["id"]) if o["status"] in ("active", "running") else None
        out_nodes[cid] = o
    for nid, n in nodes.items():
        if n["is_sub"]:
            out_nodes[nid] = to_out(n, "subagent")

    orphans = None
    for nid, n in nodes.items():
        if not n["is_sub"]:
            continue
        pid = n["parent_id"]
        if pid in nodes:
            parent = out_nodes[pid] if nodes[pid]["is_sub"] else out_nodes[root_of(pid)]
        else:
            if orphans is None:
                orphans = {"id": "", "kind": "conversation", "title": "Parent not found", "status": "unknown",
                           "profile": n["profile"] or "default", "children": [], "source": "",
                           "last_activity_at": None, "cost": 0, "tokens_in": 0, "tokens_out": 0}
            parent = orphans
        parent["children"].append(out_nodes[nid])

    roots = [o for o in out_nodes.values() if o["kind"] == "conversation"]
    roots = [o for o in roots if o["children"] or o["status"] in ("active", "running")]
    if orphans:
        roots.append(orphans)

    def finish(o):
        """Sort children and add subtree totals."""
        o["children"].sort(key=lambda c: c["started_at"] or 0)
        o["subtree"] = {"subagents": 0, "running": 0, "cost": 0.0, "tokens": 0,
                        "last_activity_at": o.get("last_activity_at") or 0}
        for c in o["children"]:
            finish(c)
            st = c["subtree"]
            o["subtree"]["subagents"] += 1 + st["subagents"]
            o["subtree"]["running"] += (c["status"] in ("running", "active")) + st["running"]
            o["subtree"]["cost"] += (c["cost"] or 0) + st["cost"]
            o["subtree"]["tokens"] += c["tokens_in"] + c["tokens_out"] + st["tokens"]
            o["subtree"]["last_activity_at"] = max(o["subtree"]["last_activity_at"], st["last_activity_at"])
        o["subtree"]["cost"] = round(o["subtree"]["cost"], 4)

    for r in roots:
        finish(r)
    roots.sort(key=lambda r: (r["subtree"]["running"] > 0, r["subtree"]["last_activity_at"]), reverse=True)
    return {"conversations": roots[:MAX_CONVERSATIONS], "total_conversations": len(roots)}


def subagent_goals(db, ctx, nodes):
    """Subagent session id -> its task, taken from resumable_subagents.

    A record is linked to a session if it mentions the session id; otherwise
    by timing: Hermes creates the record within milliseconds of starting the
    session and updates it as the session ends.
    """
    if not db.has("resumable_subagents", "record_json", "created_at", "updated_at"):
        return {}
    subs = {i: n for i, n in nodes.items() if n["is_sub"]}
    if not subs:
        return {}
    oldest = min(n["started_at"] for n in subs.values()) - MATCH_WINDOW_S
    goals, pairs = {}, []
    for r in db.rows("SELECT subagent_id, created_at, updated_at, record_json FROM resumable_subagents "
                     "WHERE created_at >= ?", (oldest,)):
        goal = pick_text(r["record_json"], ("goal", "task", "name", "title", "description"), 200)
        if not goal:
            continue
        refs = json_strings(r["record_json"]) & subs.keys()
        if refs:
            for i in refs:
                goals.setdefault(i, goal)
            continue
        for i, n in subs.items():
            start_gap = abs(n["started_at"] - r["created_at"])
            if start_gap > MATCH_WINDOW_S:
                continue
            end_gap = abs(n["ended_at"] - r["updated_at"]) if n["ended_at"] else 0
            pairs.append((start_gap + min(end_gap, 60), r["subagent_id"], i, goal))
    used = set()
    for _, rid, sid, goal in sorted(pairs):
        if rid in used or sid in goals:
            continue
        used.add(rid)
        goals[sid] = goal
    return goals


def _status(n, worker, ctx):
    if worker in ("running", "registered"):
        return "running"
    if n["ended_at"]:
        return "failed" if n["end_reason"] and FAILED.search(n["end_reason"]) else "done"
    quiet = ctx.now - (n["last_activity_at"] or n["started_at"])
    return "active" if quiet < (SUB_ACTIVE_S if n["is_sub"] else ACTIVE_S) else "idle"


# ---- background delegations & resumable subagents ------------------------------------

def delegations(db, ctx):
    if not db.has("async_delegations", "delegation_id", "state", "dispatched_at"):
        return []
    c = lambda col, default="NULL": db.col("async_delegations", col, default=default)  # noqa: E731
    rows = db.rows(
        "SELECT delegation_id AS id, %s AS parent_session_id, %s AS origin_session_id, state, "
        "       %s AS delivery_state, %s AS delivery_attempts, dispatched_at, %s AS completed_at, %s AS task_json "
        "FROM async_delegations WHERE dispatched_at >= ? OR completed_at IS NULL "
        "ORDER BY dispatched_at DESC LIMIT 50"
        % (c("parent_session_id"), c("origin_session_id"), c("delivery_state"), c("delivery_attempts", "0"),
           c("completed_at"), c("task_json")), (ctx.now - WINDOW_S,))
    for r in rows:
        r["goal"] = ctx.text(pick_text(r.pop("task_json")), 160)
        end = r["completed_at"] or ctx.now
        r["duration_s"] = round(end - r["dispatched_at"])
    return rows


def resumable(db, ctx):
    if not db.has("resumable_subagents", "subagent_id", "state", "updated_at"):
        return []
    c = lambda col, default="NULL": db.col("resumable_subagents", col, default=default)  # noqa: E731
    rows = db.rows(
        "SELECT subagent_id AS id, state, %s AS pinned, %s AS stop_cause, %s AS created_at, updated_at, "
        "       %s AS expires_at, %s AS record_json "
        "FROM resumable_subagents ORDER BY updated_at DESC LIMIT 50"
        % (c("pinned", "0"), c("stop_cause"), c("created_at"), c("expires_at"), c("record_json")))
    for r in rows:
        r["name"] = ctx.text(pick_text(r.pop("record_json"), ("name", "title", "goal", "task")), 100)
        r["pinned"] = bool(r["pinned"])
    return rows
