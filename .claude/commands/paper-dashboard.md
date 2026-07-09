Launch the MEICAgent paper-trading dashboard — the same dashboard as `/dashboard`, pointed at `data/paper_trades.db` instead of the live account. Runs on a separate port (5051) so it can be open alongside the live dashboard (5050) without conflict, and is visually marked "Paper Mode — Simulated" so it can never be mistaken for real account data.

## Step 1 — Check if already running

```bash
python -c "import socket; s=socket.socket(); r=s.connect_ex(('127.0.0.1',5051)); s.close(); print('running' if r==0 else 'not_running')"
```

## Step 2a — Already running

If output is `running`: open the browser and tell the user the paper-trading dashboard is already running.

```bash
start http://localhost:5051
```

Tell the user: "Paper-trading dashboard already running — opening browser at http://localhost:5051"

## Step 2b — Not running

If output is `not_running`: start the server as a hidden background process, then open the browser. `--mode paper` alone is sufficient — it drives both the DB path (`data/paper_trades.db`) and the port default (5051).

```bash
Start-Process python -ArgumentList 'dashboard.py','--mode','paper' -WorkingDirectory $PWD -WindowStyle Hidden
```

Wait 1 second, then open browser:
```bash
start http://localhost:5051
```

Tell the user: "Paper-trading dashboard started at http://localhost:5051 (Paper Mode — simulated data only) · auto-refreshes every 30 s"
