"""Structured log CLI helper for MEICAgent."""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(__file__))
_LOG_DIR = os.path.join(_ROOT, "logs")
_LOG_PATH = os.path.join(_LOG_DIR, "agent.log")


def _now_iso():
    try:
        import pytz
        et = pytz.timezone("America/New_York")
        from datetime import datetime as dt
        return dt.now(et).isoformat()
    except ImportError:
        return datetime.now(timezone.utc).isoformat()


def _out(data):
    print(json.dumps(data, default=str))


def _log_event_internal(level, message, data=None):
    os.makedirs(_LOG_DIR, exist_ok=True)
    entry = {"timestamp": _now_iso(), "level": level, "message": message}
    if data:
        entry["data"] = data
    with open(_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def cmd_log_event(args):
    data = None
    if args.data:
        try:
            data = json.loads(args.data)
        except json.JSONDecodeError:
            data = args.data
    _log_event_internal(args.level.upper(), args.message, data)
    _out({"ok": True})


def main():
    parser = argparse.ArgumentParser(description="MEICAgent log helper")
    sub = parser.add_subparsers(dest="command")

    p_log = sub.add_parser("log_event")
    p_log.add_argument("--level", default="INFO")
    p_log.add_argument("--message", required=True)
    p_log.add_argument("--data", default=None)

    args = parser.parse_args()
    dispatch = {"log_event": cmd_log_event}
    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        sys.exit(1)
    fn(args)


if __name__ == "__main__":
    main()
