#!/usr/bin/env python3
"""Make a fake Hermes state.db (real schema, made-up data) for trying the dashboard.

    python3 tools/fake_hermes.py --db /tmp/hmc/state.db            # create 14 days of history
    python3 tools/fake_hermes.py --db /tmp/hmc/state.db --live     # ...and keep it busy

--live keeps the gateway heartbeat fresh, adds messages to active
conversations, and starts and finishes subagents, so every status shows up.

Private-looking fields (message text, system prompts, delivery content,
task payloads beyond the goal) contain PRIVATE_MARKER, so tests can check
that none of it ever reaches a report.
"""
import argparse
import json
import os
import random
import sqlite3
import time
import uuid

PRIVATE_MARKER = "FAKE-PRIVATE-TEXT-7f3a"
SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
SCHEMA_VERSION = 41

PLATFORMS = [("telegram", 6), ("discord", 3), ("cli", 2), ("cron", 2), ("slack", 1)]
MODELS = [("anthropic/claude-sonnet-4.5", 5), ("nous/hermes-4-405b", 3), ("openai/gpt-5-mini", 2)]
SUB_MODELS = [("openai/gpt-5-mini", 4), ("nous/hermes-4-70b", 3), ("anthropic/claude-haiku-4.5", 2)]
PRICES = {  # $ per million input / output tokens (made up)
    "anthropic/claude-sonnet-4.5": (3.0, 15.0), "nous/hermes-4-405b": (1.0, 3.0),
    "openai/gpt-5-mini": (0.25, 2.0), "nous/hermes-4-70b": (0.13, 0.4), "anthropic/claude-haiku-4.5": (1.0, 5.0),
}
TOOLS = [("terminal", 9), ("web_search", 6), ("read_file", 6), ("write_file", 3), ("browser_navigate", 3),
         ("delegate_task", 2), ("memory", 2), ("skill_view", 2), ("send_message", 1), ("vision_analyze", 1)]
TITLES = ["Plan weekend garage cleanup", "Compare NAS options", "Fix Tailscale DNS on laptop",
          "Summarise RSS feeds", "Draft email to landlord", "Research heat pump rebates",
          "Refactor garage-monitor agent", "Track package deliveries", "Morning news digest",
          "Find a 3D printer filament deal", "Back up photos to NAS", "Debug Home Assistant automation"]
GOALS = ["Search the web for current prices and summarise the top 3", "Read the repo and list TODOs",
         "Check the logs for errors in the last day", "Write unit tests for the parser",
         "Compare three options in a table", "Find the official documentation page",
         "Draft a short reply", "Verify the backup completed"]
ACTIVITIES = ["Running terminal command", "Searching the web", "Reading files", "Waiting for model",
              "Writing file", "Delegating to subagents", "Browsing page"]


def pick(weighted):
    items, weights = zip(*weighted)
    return random.choices(items, weights=weights)[0]


def new_id(prefix):
    return "%s_%s" % (prefix, uuid.uuid4().hex[:16])


class Fake:
    def __init__(self, path):
        self.path = path
        fresh = not os.path.exists(path)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        if fresh:
            with open(SCHEMA) as f:
                self.db.executescript(f.read())
            self.db.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))

    # ---- building blocks -------------------------------------------------------

    def session(self, started, source, model, parent=None, delegate_from=None, profile=None,
                ended=None, end_reason=None, title=None, activity_at=None, n_messages=8):
        sid = new_id("sess")
        model_config = {"temperature": 0.7}
        if delegate_from:
            model_config["_delegate_from"] = delegate_from
        tin = random.randint(2_000, 60_000) * max(1, n_messages // 4)
        tout = int(tin * random.uniform(0.05, 0.25))
        pin, pout = PRICES.get(model, (1, 3))
        cost = tin / 1e6 * pin + tout / 1e6 * pout
        tools = random.sample([t for t, _ in TOOLS], k=random.randint(1, 4))
        activity_at = activity_at or ended or started + 60
        self.db.execute(
            "INSERT INTO sessions (id, source, created_source, user_id, chat_id, display_name, model, model_config, "
            "system_prompt, parent_session_id, started_at, ended_at, end_reason, message_count, tool_call_count, "
            "input_tokens, output_tokens, cache_read_tokens, reasoning_tokens, estimated_cost_usd, cost_status, "
            "title, last_activity_at, last_activity_description, api_call_count, profile_name, tool_names) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, source, source, "user-" + PRIVATE_MARKER, "chat-" + PRIVATE_MARKER, "Name " + PRIVATE_MARKER,
             model, json.dumps(model_config), "System prompt " + PRIVATE_MARKER, parent, started, ended,
             end_reason, n_messages, 0, tin, tout, int(tin * 0.6), int(tout * 0.3), round(cost, 6), "estimated",
             title, activity_at, random.choice(ACTIVITIES), max(1, n_messages // 2), profile, json.dumps(tools)))
        self.db.execute(
            "INSERT INTO session_model_usage (session_id, model, task, api_call_count, input_tokens, output_tokens, "
            "cache_read_tokens, reasoning_tokens, estimated_cost_usd, first_seen, last_seen) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, model, "", max(1, n_messages // 2), tin, tout, int(tin * 0.6), int(tout * 0.3), round(cost, 6),
             started, activity_at))
        if random.random() < 0.2:  # auxiliary model use, e.g. context compression
            self.db.execute(
                "INSERT INTO session_model_usage (session_id, model, task, api_call_count, input_tokens, "
                "output_tokens, estimated_cost_usd, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, "openai/gpt-5-mini", "compression", 1, tin // 4, tin // 40, round(tin / 4e6 * 0.25, 6),
                 started, activity_at))
        self.messages(sid, started, activity_at, n_messages)
        return sid

    def messages(self, sid, start, end, n):
        t = start
        step = max(1.0, (end - start) / max(1, n))
        calls = 0
        for i in range(n):
            role = "user" if i % 4 == 0 else random.choice(["assistant", "tool", "tool"])
            tool = pick(TOOLS) if role == "tool" else None
            disp = None
            if tool:
                calls += 1
                disp = "failed" if random.random() < 0.07 else "succeeded"
            self.db.execute(
                "INSERT INTO messages (session_id, role, content, tool_name, effect_disposition, timestamp, "
                "reasoning) VALUES (?,?,?,?,?,?,?)",
                (sid, role, "Message text %s #%d" % (PRIVATE_MARKER, i), tool, disp, t,
                 "Reasoning " + PRIVATE_MARKER if role == "assistant" else None))
            t = min(end, t + step * random.uniform(0.5, 1.5))
        self.db.execute("UPDATE sessions SET tool_call_count = tool_call_count + ? WHERE id = ?", (calls, sid))

    def subagents(self, parent, started, ended, depth=0, running=False):
        for _ in range(random.randint(1, 4) if depth == 0 else random.randint(1, 2)):
            s = min(started + random.uniform(5, 120), time.time() - 1)
            if running:
                e, reason, act = None, None, time.time() - random.uniform(0, 60)
            else:
                e = s + random.uniform(20, 600)
                if ended:
                    e = min(e, ended)
                reason = "error: tool budget exceeded" if random.random() < 0.08 else "agent_close"
                act = e
            self.sub_count = getattr(self, "sub_count", 0) + 1
            sid = self.session(s, "subagent", pick(SUB_MODELS), parent=parent, delegate_from=parent,
                               ended=e, end_reason=reason, title="Subagent: CONTEXT: #%d" % self.sub_count,
                               activity_at=act, n_messages=random.randint(4, 14), profile=None)
            # Like real Hermes: the record doesn't name the session; it's created
            # just after the session starts and updated as it ends.
            self.db.execute(
                "INSERT INTO resumable_subagents VALUES (?,?,?,?,?,?,?,?,?,?)",
                (new_id("sa"), "running" if running else "completed", 0, None, None, None,
                 json.dumps({"goal": random.choice(GOALS), "history": "History " + PRIVATE_MARKER}),
                 s + 0.05, (e or s) + 0.01, (e or s) + 172800))
            if running:
                self.worker(sid, "child", "running")
            if depth == 0 and random.random() < 0.15:
                self.subagents(sid, s, e, depth + 1, running=running)
            if random.random() < 0.3:
                self.delegation(parent, s, e)

    def worker(self, sid, kind, status):
        self.db.execute("INSERT INTO worker_executions VALUES (?,?,?,?,?,?,?,?)",
                        (new_id("exec"), sid, kind, 1, 1, status, "digest", 0))

    def delegation(self, parent, dispatched, completed):
        state = "completed" if completed else "running"
        self.db.execute(
            "INSERT INTO async_delegations (delegation_id, origin_session, parent_session_id, state, dispatched_at, "
            "completed_at, updated_at, task_json, result_json, delivery_state, delivery_attempts, delivered_at, "
            "origin_session_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id("dlg"), parent, parent, state, dispatched, completed, completed or dispatched,
             json.dumps({"goal": random.choice(GOALS), "context": "Context " + PRIVATE_MARKER}),
             json.dumps({"summary": "Result " + PRIVATE_MARKER}) if completed else None,
             "delivered" if completed else "pending", 1 if completed else 0, completed, parent))

    def conversation(self, started, active=False):
        source = pick(PLATFORMS)
        profile = "research" if random.random() < 0.2 else None
        n = random.randint(6, 30)
        if active:
            ended, act = None, time.time() - random.uniform(0, 300)
        else:
            ended = started + random.uniform(120, 3600)
            act = ended
        sid = self.session(started, source, pick(MODELS), profile=profile, ended=ended,
                           end_reason="user_exit" if ended else None, title=random.choice(TITLES),
                           activity_at=act, n_messages=n)
        if not active and random.random() < 0.15:  # continued after context compression
            self.db.execute("UPDATE sessions SET end_reason = 'compression' WHERE id = ?", (sid,))
            sid2 = self.session(ended, source, pick(MODELS), parent=sid, profile=profile,
                                ended=ended + 900, end_reason="user_exit", activity_at=ended + 900, n_messages=10)
            self.subagents(sid2, ended, ended + 900)
        elif not active and random.random() < 0.1:  # user started over: a new conversation
            self.db.execute("UPDATE sessions SET end_reason = 'session_reset' WHERE id = ?", (sid,))
            sid2 = self.session(ended, source, pick(MODELS), parent=sid, profile=profile, title=random.choice(TITLES),
                                ended=ended + 600, end_reason="user_exit", activity_at=ended + 600, n_messages=8)
            self.subagents(sid2, ended, ended + 600)
        if random.random() < 0.35 or active:
            self.subagents(sid, started, ended, running=active)
        if source == "cron":
            self.worker(sid, "cron", "terminal")
        return sid

    # ---- whole database ----------------------------------------------------------

    def populate(self, days=14):
        random.seed(7)
        now = time.time()
        self.db.execute("BEGIN")
        for d in range(days, 0, -1):
            for _ in range(random.randint(5, 14)):
                started = now - d * 86400 + random.uniform(0, 86400 - 4000)
                self.conversation(started)
        for _ in range(2):
            self.conversation(now - random.uniform(600, 3600), active=True)
        for i in range(3):
            self.db.execute(
                "INSERT INTO resumable_subagents VALUES (?,?,?,?,?,?,?,?,?,?)",
                (new_id("sub"), random.choice(["paused", "ready", "expired"]), int(i == 0), "idle_timeout",
                 None, None, json.dumps({"name": ["nightly-digest", "repo-watcher", "price-tracker"][i],
                                         "history": "History " + PRIVATE_MARKER}),
                 now - 86400 * (i + 1), now - 3600 * (i + 1), now + 86400 * 3))
        self.db.execute(
            "INSERT INTO delivery_obligations (obligation_id, session_key, platform, chat_id, content, state, "
            "attempts, created_at, updated_at, last_error) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (new_id("dlv"), "key", "telegram", "chat", "Outgoing " + PRIVATE_MARKER, "failed", 3,
             now - 7200, now - 7000, "HTTP 502 from api.telegram.org"))
        self.db.execute("INSERT INTO gateway_hygiene_state VALUES ('telegram:123', 2)")
        self.db.execute("INSERT INTO session_admissions (admission_id, request_id, principal_id, target_session_id, "
                        "lineage_json, payload_json, payload_digest, intent, status) "
                        "SELECT 'adm1', 'req1', 'cron-owner', id, '{}', ?, 'd', 'queue', 'queued' "
                        "FROM sessions WHERE ended_at IS NULL LIMIT 1", (json.dumps({"text": PRIVATE_MARKER}),))
        self.heartbeat(now - 5 * 3600)
        self.db.execute("COMMIT")

    def heartbeat(self, started=None):
        now = time.time()
        if started is not None:
            self.db.execute("INSERT OR REPLACE INTO gateway_heartbeats VALUES ('gw-main', 4242, ?, ?, '', 'hermes-box')",
                            (started, now))
        else:
            self.db.execute("UPDATE gateway_heartbeats SET last_heartbeat = ? WHERE backend_id = 'gw-main'", (now,))

    def tick(self):
        """One step of --live: keep things moving."""
        now = time.time()
        self.heartbeat()
        active = [r[0] for r in self.db.execute(
            "SELECT id FROM sessions WHERE ended_at IS NULL AND source != 'subagent'")]
        if len(active) < 2 or random.random() < 0.03:
            active.append(self.conversation(now - 30, active=True))
        for sid in active:
            if random.random() < 0.5:
                self.messages(sid, now, now, 1)
                self.db.execute("UPDATE sessions SET last_activity_at = ?, last_activity_description = ?, "
                                "message_count = message_count + 1, input_tokens = input_tokens + 1500, "
                                "estimated_cost_usd = estimated_cost_usd + 0.004 WHERE id = ?",
                                (now, random.choice(ACTIVITIES), sid))
            if random.random() < 0.05:
                self.subagents(sid, now - 100, None, running=True)
        # Finish running subagents after a while; occasionally one fails.
        for sid, started in self.db.execute(
                "SELECT id, started_at FROM sessions WHERE source = 'subagent' AND ended_at IS NULL").fetchall():
            if now - started > random.uniform(40, 240):
                reason = "error: model timeout" if random.random() < 0.1 else "agent_close"
                self.db.execute("UPDATE sessions SET ended_at = ?, end_reason = ?, last_activity_at = ? WHERE id = ?",
                                (now, reason, now, sid))
                self.db.execute("UPDATE resumable_subagents SET state = 'completed', updated_at = ? "
                                "WHERE state = 'running' AND abs(created_at - ?) < 1", (now, started))
                self.db.execute("UPDATE worker_executions SET status = 'terminal' WHERE session_id = ?", (sid,))
            elif random.random() < 0.4:
                self.messages(sid, now, now, 1)
                self.db.execute("UPDATE sessions SET last_activity_at = ?, last_activity_description = ? WHERE id = ?",
                                (now, random.choice(ACTIVITIES), sid))
        # End a conversation now and then.
        if len(active) > 3 and random.random() < 0.05:
            self.db.execute("UPDATE sessions SET ended_at = ?, end_reason = 'user_exit' WHERE id = ?",
                            (now, random.choice(active)))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="path of the fake state.db to create or update")
    ap.add_argument("--live", action="store_true", help="keep updating it every 2 seconds")
    args = ap.parse_args()
    fake = Fake(args.db)
    if fake.db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0:
        fake.populate()
        print("created %s" % args.db, flush=True)
    if args.live:
        print("live mode: updating every 2 s (Ctrl+C to stop)", flush=True)
        try:
            while True:
                fake.tick()
                time.sleep(2)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
