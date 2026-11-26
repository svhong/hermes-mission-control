"""Tab registry.

Every module in this folder (not starting with "_") is one dashboard tab.
A tab module defines:

    TAB = {"id": "overview", "title": "Overview", "order": 10}
    INTERVAL = 10          # optional: seconds between refreshes (default 5)

    def collect(db, ctx):  # db: HermesDB (open), ctx: Context
        return {...}       # JSON-safe dict, metadata only

and has a matching renderer at hub/static/tabs/<id>.js. To add a feature,
add those two files; nothing else needs to change.
"""
import importlib
import pkgutil
import re
import sys
import time
import traceback

from hermes_db import truncate

_ID = re.compile(r"^[a-z0-9_-]{1,32}$")


def discover():
    tabs = []
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module("%s.%s" % (__name__, info.name))
        except Exception:
            print("tab %s failed to load:\n%s" % (info.name, traceback.format_exc()), file=sys.stderr, flush=True)
            continue
        meta = getattr(mod, "TAB", None)
        if not isinstance(meta, dict) or not _ID.match(str(meta.get("id", ""))) or not callable(getattr(mod, "collect", None)):
            print("tab %s skipped: needs TAB = {id, title} and collect(db, ctx)" % info.name, file=sys.stderr, flush=True)
            continue
        tabs.append(mod)
    tabs.sort(key=lambda m: (m.TAB.get("order", 100), m.TAB["id"]))
    return tabs


class Context:
    """Per-cycle values shared by every tab."""

    def __init__(self, now=None, show_text=True):
        self.now = now if now is not None else time.time()
        self.show_text = show_text
        lt = time.localtime(self.now)
        self.day_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))

    def text(self, value, limit=160):
        """Free text (titles, activity, task goals) only when allowed."""
        if not self.show_text or value is None:
            return None
        return truncate(value, limit)


def section(fn, *args):
    """Run one part of a tab; a failure becomes {"_error": ...} instead of
    taking the whole tab down (e.g. after a Hermes schema change)."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001 - reported to the dashboard
        return {"_error": "%s: %s" % (type(e).__name__, e)}
