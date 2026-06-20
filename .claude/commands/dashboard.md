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

If output is `not_running`: start the server as a hidden background process, then open the browser.

```bash
Start-Process python -ArgumentList 'dashboard.py' -WorkingDirectory $PWD -WindowStyle Hidden
```

Wait 1 second, then open browser:
```bash
start http://localhost:5050
```

Tell the user: "Dashboard started at http://localhost:5050 · auto-refreshes every 30 s"