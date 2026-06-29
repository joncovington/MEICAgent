"""Email and structured log CLI helper for MEICAgent."""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

_ENV_VAR_MAP = {
    "sendgrid_api_key": "MEICAGENT_SENDGRID_KEY",
}

try:
    import keyring as _keyring
    def _get_secret(name: str) -> str | None:
        value = _keyring.get_password("meicagent", name)
        if value:
            return value
        return os.environ.get(_ENV_VAR_MAP.get(name, ""))
except ImportError:
    def _get_secret(name: str) -> str | None:
        return os.environ.get(_ENV_VAR_MAP.get(name, ""))

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
_LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
_LOG_PATH = os.path.join(_LOG_DIR, "agent.log")


def _load_config():
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


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


# ---------------------------------------------------------------------------
# Email senders
# ---------------------------------------------------------------------------

def _send_email(subject, body):
    cfg = _load_config()
    cfg_email = cfg.get("email", {})
    if not cfg_email.get("enabled", False):
        _log_event_internal("INFO", f"Email disabled — would send: {subject}")
        return
    api_key = _get_secret("sendgrid_api_key")
    if not api_key:
        raise RuntimeError(
            "SendGrid API key not found. Store it via the OS keyring:\n"
            "  python -c \"import keyring; keyring.set_password('meicagent', 'sendgrid_api_key', 'YOUR_KEY')\"\n"
            "Or set the environment variable: MEICAGENT_SENDGRID_KEY=YOUR_KEY"
        )
    payload = json.dumps({
        "personalizations": [{"to": [{"email": cfg_email["to"]}]}],
        "from": {"email": cfg_email["from"]},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        if resp.status not in (200, 202):
            raise RuntimeError(f"SendGrid returned {resp.status}")


# ---------------------------------------------------------------------------
# Log helper
# ---------------------------------------------------------------------------

def _log_event_internal(level, message, data=None):
    os.makedirs(_LOG_DIR, exist_ok=True)
    entry = {"timestamp": _now_iso(), "level": level, "message": message}
    if data:
        entry["data"] = data
    with open(_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_send_alert(args):
    try:
        _send_email(args.subject, args.body)
        _log_event_internal("INFO", f"Alert sent: {args.subject}")
        _out({"ok": True})
    except Exception as exc:
        _log_event_internal("ERROR", f"Alert failed: {exc}")
        _out({"ok": False, "error": str(exc)})


def cmd_send_eod_email(_args):
    try:
        result = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(__file__), "db.py"), "get_eod_summary"],
            capture_output=True, text=True, check=True,
        )
        summary = json.loads(result.stdout)
    except Exception as exc:
        _out({"ok": False, "error": f"Could not get EOD summary: {exc}"})
        return

    d = summary
    lines = [
        f"MEICAgent EOD Summary — {d.get('date', 'unknown')}",
        "",
        f"Entries:   {d.get('total_entries', 0)} total | {d.get('entries_filled', 0)} filled | "
        f"{d.get('entries_stopped', 0)} stopped | {d.get('entries_expired', 0)} expired | "
        f"{d.get('entries_cancelled', 0)} cancelled",
        f"P&L:       gross={d.get('gross_pnl', 0):.2f}  fees={d.get('fees', 0):.2f}  "
        f"net={d.get('net_pnl', 0):.2f}",
        f"Win rate:  {d.get('win_count', 0)} wins / {d.get('entries_filled', 0)} filled "
        f"({d.get('win_rate_pct') or 0:.1f}%)",
        f"Avg IV:    {d.get('avg_iv_rank') or 'n/a'}",
        f"Sessions:  {', '.join(d.get('sessions_entered', [])) or 'none'}",
    ]
    if d.get("trades"):
        lines += ["", "--- Trades ---"]
        for t in d["trades"]:
            lines.append(
                f"  {t.get('ic_order_id','?')} | {t.get('status','?')} | "
                f"credit={t.get('net_credit') or 0:.2f} | pnl={t.get('pnl') or 0:.2f} | "
                f"{t.get('session_quality','?')} | {t.get('iv_skew_signal','?')} | {t.get('price_action_signal','?')}"
            )
            if t.get("ai_entry_reasoning"):
                lines.append(f"    entry: {t['ai_entry_reasoning']}")
            if t.get("exit_analysis"):
                lines.append(f"    exit:  {t['exit_analysis']}")

    if d.get("ai_day_summary"):
        lines += ["", "=" * 60, "AGENT ANALYSIS", "=" * 60, "", d["ai_day_summary"]]

    body = "\n".join(lines)
    subject = f"MEICAgent EOD {d.get('date', '')} | net P&L ${d.get('net_pnl', 0):.2f}"
    try:
        _send_email(subject, body)
        _log_event_internal("INFO", f"EOD email sent for {d.get('date')}")
        _out({"ok": True})
    except Exception as exc:
        _log_event_internal("ERROR", f"EOD email failed: {exc}")
        _out({"ok": False, "error": str(exc)})


def cmd_log_event(args):
    data = None
    if args.data:
        try:
            data = json.loads(args.data)
        except json.JSONDecodeError:
            data = args.data
    _log_event_internal(args.level.upper(), args.message, data)
    _out({"ok": True})


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="MEICAgent notify helper")
    sub = parser.add_subparsers(dest="command")

    p_alert = sub.add_parser("send_alert")
    p_alert.add_argument("--subject", required=True)
    p_alert.add_argument("--body", required=True)

    sub.add_parser("send_eod_email")

    p_log = sub.add_parser("log_event")
    p_log.add_argument("--level", default="INFO")
    p_log.add_argument("--message", required=True)
    p_log.add_argument("--data", default=None)

    args = parser.parse_args()
    dispatch = {
        "send_alert": cmd_send_alert,
        "send_eod_email": cmd_send_eod_email,
        "log_event": cmd_log_event,
    }
    fn = dispatch.get(args.command)
    if fn is None:
        parser.print_help()
        sys.exit(1)
    fn(args)


if __name__ == "__main__":
    main()
