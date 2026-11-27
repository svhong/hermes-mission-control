#!/usr/bin/env python3
"""Container health check for the hub, the collector and the container monitor.

  healthcheck.py hub              the hub answers /api/health
  healthcheck.py file PATH [MAX]  PATH was touched in the last MAX seconds (default 180);
                                  the collector touches it after every exchange with the hub
"""
import os
import sys
import time
import urllib.request


def main(args):
    if args[:1] == ["hub"]:
        port = os.environ.get("HMC_PORT", "8788")
        with urllib.request.urlopen("http://127.0.0.1:%s/api/health" % port, timeout=3) as r:
            return 0 if r.status == 200 else 1
    if args[:1] == ["file"] and len(args) >= 2:
        limit = float(args[2]) if len(args) > 2 else 180
        try:
            age = time.time() - os.path.getmtime(args[1])
        except OSError:
            print("no exchange with the hub yet")
            return 1
        if age > limit:
            print("last exchange with the hub %ds ago" % age)
            return 1
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:  # noqa: BLE001
        print(e)
        sys.exit(1)
