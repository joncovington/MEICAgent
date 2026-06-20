Launch the MEICAgent intraday dashboard.

## Step 1 — Check if already running

```bash
python -c "import socket; s=socket.socket(); r=s.connect_ex(('127.0.0.1',5050)); s.close(); print('running' if r==0 else 'not_running')"
```

## Step 2a — Already running

If output is `running`: open the browser and tell the user the dashboard is already running.

```bash
start http://localhost:5050
```

Tell the user: "Dashboard already running — opening browser at http://localhost:5050"

## Step 2b — Not running

If output is `not_running`: open a new terminal window running the server, then open the browser.

```bash
Start-Process powershell -ArgumentList '-NoExit', '-Command', '$host.UI.RawUI.WindowTitle = ''MEICAgent Dashboard''; cd \"c:\Users\jonco\Claude\MEICAgent\"; python dashboard.py'
```

Wait 1 second, then open browser:
```bash
start http://localhost:5050
```

Tell the user: "Dashboard started in a new terminal window at http://localhost:5050 · auto-refreshes every 30 s · close that terminal window to stop it"