"""MEICAgent Watchdog — alerts when the loop stops running during market hours."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta

# ── Timezone helpers ──────────────────────────────────────────────────────────

try:
    import pytz as _pytz
    _ET = _pytz.timezone("America/New_York")
    def _now_et() -> datetime:
        return datetime.now(_ET)
except ImportError:
    from datetime import timezone as _timezone
    def _now_et() -> datetime:
        return datetime.now(_timezone.utc)

# ── Paths ─────────────────────────────────────────────────────────────────────

_ROOT    = os.path.dirname(os.path.abspath(__file__))
_DB_PATH = os.path.join(_ROOT, "data", "meic_trades.db")
_CFG     = os.path.join(_ROOT, "config.json")


def _load_config() -> dict:
    with open(_CFG, encoding="utf-8") as f:
        return json.load(f)


# ── Market hours ──────────────────────────────────────────────────────────────

def _is_market_hours(now: datetime, cfg: dict) -> bool:
    if now.weekday() >= 5:
        return False
    year_key = f"nyse_holidays_{now.year}"
    holidays = cfg.get(year_key, [])
    if now.strftime("%Y-%m-%d") in holidays:
        return False
    t = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= t <= (15 * 60 + 55)


# ── DB ────────────────────────────────────────────────────────────────────────

def _last_loop_dt() -> datetime | None:
    if not os.path.exists(_DB_PATH):
        return None
    try:
        conn = sqlite3.connect(_DB_PATH)
        row = conn.execute(
            "SELECT loop_time FROM loop_log ORDER BY loop_time DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if not row:
            return None
        ts = str(row[0]).replace(" ", "T")
        # parse as naive then attach ET if no tz info
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            return None
        if dt.tzinfo is None:
            try:
                import pytz
                dt = pytz.timezone("America/New_York").localize(dt)
            except ImportError:
                pass
        return dt
    except sqlite3.Error:
        return None


# ── Alerts ────────────────────────────────────────────────────────────────────

def _send_email(subject: str, body: str) -> None:
    try:
        subprocess.run(
            [sys.executable, os.path.join(_ROOT, "notify.py"),
             "send_alert", f"--subject={subject}", f"--body={body}"],
            timeout=30,
        )
    except Exception as exc:
        print(f"[watchdog] email alert failed: {exc}")


def _send_toast(title: str, message: str) -> None:
    # Uses Windows Runtime toast — no extra packages required on Windows 10/11
    safe_title   = title.replace("'", "\\'")
    safe_message = message.replace("'", "\\'")
    ps = (
        "[void][Windows.UI.Notifications.ToastNotificationManager,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime];"
        "$t=[Windows.UI.Notifications.ToastTemplateType]::ToastText02;"
        "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($t);"
        f"$x.GetElementsByTagName('text')[0].InnerText='{safe_title}';"
        f"$x.GetElementsByTagName('text')[1].InnerText='{safe_message}';"
        "$n=[Windows.UI.Notifications.ToastNotification]::new($x);"
        "[Windows.UI.Notifications.ToastNotificationManager]"
        "::CreateToastNotifier('MEICAgent').Show($n)"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            timeout=10,
        )
    except Exception as exc:
        print(f"[watchdog] toast failed: {exc}")


def _alert(now: datetime, last: datetime | None, stale_after: int) -> None:
    if last is None:
        detail = "No loop activity recorded today."
    else:
        age_min = int((now - last).total_seconds() / 60)
        detail = f"Last loop entry was {age_min} minutes ago (at {last.strftime('%H:%M')} ET)."

    subject = "MEICAgent — loop stopped during market hours"
    body    = (
        f"The MEICAgent loop has not run in the last {stale_after} minutes.\n"
        f"{detail}\n\n"
        "Open positions are protected by DAY stop orders but are not being actively managed.\n"
        "Restart Claude Code and run /loop to resume."
    )

    print(f"[watchdog] ALERT: {detail}")
    _send_email(subject, body)
    _send_toast("MEICAgent Watchdog", f"Loop stopped — {detail} Restart Claude Code.")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="MEICAgent Watchdog")
    parser.add_argument("--interval",    type=int, default=5,
                        help="Check interval in minutes (default: 5)")
    parser.add_argument("--stale-after", type=int, default=15,
                        help="Minutes without a loop entry before alerting (default: 15)")
    parser.add_argument("--cooldown",    type=int, default=30,
                        help="Minutes between repeated alerts (default: 30)")
    args = parser.parse_args()

    interval_s    = args.interval * 60
    stale_delta   = timedelta(minutes=args.stale_after)
    cooldown_delta = timedelta(minutes=args.cooldown)

    print(
        f"[watchdog] started — checking every {args.interval} min, "
        f"alert if stale > {args.stale_after} min, cooldown {args.cooldown} min"
    )

    last_alerted: datetime | None = None

    while True:
        try:
            cfg = _load_config()
            now = _now_et()

            if _is_market_hours(now, cfg):
                last_loop = _last_loop_dt()
                stale = (last_loop is None) or ((now - last_loop) > stale_delta)
                cooled = (last_alerted is None) or ((now - last_alerted) > cooldown_delta)

                if stale and cooled:
                    _alert(now, last_loop, args.stale_after)
                    last_alerted = now
                elif not stale:
                    age_min = int((now - last_loop).total_seconds() / 60)
                    print(f"[watchdog] {now.strftime('%H:%M')} ET — loop OK, last entry {age_min} min ago")
            else:
                print(f"[watchdog] {now.strftime('%H:%M')} ET — outside market hours, skipping check")

        except Exception as exc:
            print(f"[watchdog] error during check: {exc}")

        time.sleep(interval_s)


if __name__ == "__main__":
    main()
