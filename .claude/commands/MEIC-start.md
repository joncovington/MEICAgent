Start the full MEICAgent session: watchdog, dashboard, and agent loop.

## Step 1 — Watchdog

Launch the watchdog in a new terminal:

```bash
Start-Process powershell -WorkingDirectory 'c:\Users\jonco\Claude\MEICAgent' -ArgumentList '-NoExit', '-Command', '$host.UI.RawUI.WindowTitle = ''MEICAgent Watchdog''; python watchdog.py'
```

## Step 2 — Dashboard

Check if dashboard is already running:

```bash
python -c "import socket; s=socket.socket(); r=s.connect_ex(('127.0.0.1',5050)); s.close(); print('running' if r==0 else 'not_running')"
```

If `not_running`: launch dashboard in a new terminal, then open the browser.

```bash
Start-Process powershell -WorkingDirectory 'c:\Users\jonco\Claude\MEICAgent' -ArgumentList '-NoExit', '-Command', '$host.UI.RawUI.WindowTitle = ''MEICAgent Dashboard''; python dashboard.py'
```

```bash
start http://localhost:5050
```

If `running`: just open the browser.

```bash
start http://localhost:5050
```

## Step 3 — Agent loop

Invoke the `/loop` skill with the prompt:

> Execute the next MEIC agent loop iteration following the operating instructions in CLAUDE.md.

Tell the user:
"Startup complete — watchdog running, dashboard at http://localhost:5050, agent loop started. The loop will self-pace each iteration."