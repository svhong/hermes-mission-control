"""Read-only access to Hermes's state.db.

Rules this module keeps:
  * The database is only ever opened read-only (mode=ro, query_only).
  * The schema is looked up on every open, and callers ask has()/col()
    before using a table or column, because Hermes changes its schema often.
  * Nothing here reads message text, system prompts, reasoning or payloads.
"""
import json
import os
import sqlite3
import urllib.parse

# Hermes's own rule for "this session is a delegated subagent", copied from
# the messages_fts_trigram_src view in state.db.
_DELEGATE_FROM = (
    "json_extract(CASE WHEN json_valid({a}.model_config) THEN {a}.model_config END, '$._delegate_from')"
)


class HermesDB:
    def __init__(self, path):
        self.path = path
        self.conn = None
        self.tables = {}  # table name -> set of column names
        self.schema_version = None
        self.immutable = False

    # ---- open / close -----------------------------------------------------

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    def open(self):
        if not os.path.exists(self.path):
            raise FileNotFoundError("Hermes database not found: %s" % self.path)
        uri = "file:%s?mode=ro" % urllib.parse.quote(os.path.abspath(self.path))
        try:
            conn = self._connect(uri)
            self.immutable = False
        except sqlite3.OperationalError:
            # In WAL mode a read-only reader needs the -shm file. If Hermes is
            # stopped, that file may be gone and we aren't allowed to create
            # it. With no writer running, reading the file as immutable is safe;
            # we reopen on every cycle, so we switch back once Hermes is up.
            conn = self._connect(uri + "&immutable=1")
            self.immutable = True
        self.conn = conn
        self._load_schema()

    @staticmethod
    def _connect(uri):
        conn = sqlite3.connect(uri, uri=True, timeout=2.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = 1")
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _load_schema(self):
        self.tables = {}
        names = [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")]
        for name in names:
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(%s)" % _ident(name))}
            self.tables[name] = cols
        self.schema_version = None
        if self.has("schema_version", "version"):
            row = self.conn.execute("SELECT max(version) FROM schema_version").fetchone()
            self.schema_version = row[0] if row else None

    # ---- schema checks ------------------------------------------------------

    def has(self, table, *cols):
        have = self.tables.get(table)
        return have is not None and all(c in have for c in cols)

    def col(self, table, column, alias="", default="NULL"):
        """SQL for table.column if it exists, otherwise a default literal."""
        if self.has(table, column):
            return "%s%s" % (alias + "." if alias else "", column)
        return default

    def is_subagent_sql(self, alias="s"):
        """SQL condition that is true for delegated subagent sessions."""
        parts = ["%s.source = 'subagent'" % alias]
        if self.has("sessions", "model_config"):
            parts.append(_DELEGATE_FROM.format(a=alias) + " IS NOT NULL")
        return "(" + " OR ".join(parts) + ")"

    def delegate_from_sql(self, alias="s"):
        if self.has("sessions", "model_config"):
            return _DELEGATE_FROM.format(a=alias)
        return "NULL"

    def visible_sql(self, alias="s"):
        """SQL condition hiding archived/hidden sessions when those columns exist."""
        parts = []
        for c in ("archived", "hidden"):
            if self.has("sessions", c):
                parts.append("COALESCE(%s.%s, 0) = 0" % (alias, c))
        return "(" + " AND ".join(parts) + ")" if parts else "1"

    # ---- queries ------------------------------------------------------------

    def rows(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params)]

    def one(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row is not None else None


def _ident(name):
    return '"%s"' % name.replace('"', '""')


def parse_list(value, limit=12):
    """tool_names may be a JSON list or a comma-separated string."""
    if not value:
        return []
    try:
        items = json.loads(value)
        if isinstance(items, dict):
            items = list(items.keys())
        if not isinstance(items, list):
            items = [items]
    except (ValueError, TypeError):
        items = str(value).split(",")
    out = []
    for it in items:
        s = str(it).strip()
        if s and s not in out:
            out.append(s[:60])
    return out[:limit]


def pick_text(json_text, keys=("goal", "task", "title", "name", "description", "prompt"), limit=160):
    """A short human summary from a JSON blob (e.g. a delegation's task), or None.

    Only the first matching key is used and it's truncated, so long payloads
    never reach the dashboard.
    """
    if not json_text:
        return None
    try:
        obj = json.loads(json_text)
    except (ValueError, TypeError):
        return None
    for _ in range(3):  # look a couple of levels down, e.g. {"task": {"goal": ...}}
        if not isinstance(obj, dict):
            return None
        nested = None
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return truncate(v, limit)
            if isinstance(v, dict) and nested is None:
                nested = v
        obj = nested
    return None


def truncate(text, limit):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
