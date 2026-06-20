Start the MEICAgent watchdog in a new terminal window.

The watchdog monitors loop_log and alerts (email + Windows toast) if no loop
entry is recorded for 15 minutes during market hours. Run it alongside the
agent loop — one terminal for the loop, one for the watchdog.

## Step 1 — Check if already running

```bash
python -c "
import sqlite3, os, datetime
db = os.path.join(os.path.dirname(os.path.abspath('watchdog.py')), 'data', 'meic_trades.db')
print('db_exists' if os.path.exists(db) else 'no_db')
"
```

## Step 2 — Start in a new terminal window

```bash
Start-Process powershell -ArgumentList '-NoExit', '-Command', '$host.UI.RawUI.WindowTitle = ''MEICAgent Watchdog''; cd \"c:\Users\jonco\Claude\MEICAgent\"; python watchdog.py'
```

Tell the user: "Watchdog started in a new terminal — alerts via email and Windows toast if the loop stops for 15+ minutes during market hours. Close that terminal to stop it."