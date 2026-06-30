Start the full MEICAgent session: dashboard, streamer, and agent loop.

## Step 1 — Dashboard

Check if dashboard is already running:

```bash
python -c "import socket; s=socket.socket(); r=s.connect_ex(('127.0.0.1',5050)); s.close(); print('running' if r==0 else 'not_running')"
```

If `not_running`: launch dashboard as a hidden background process. `src\dashboard.py` opens the browser automatically on startup — do not open it again.

```bash
Start-Process python -ArgumentList 'src\dashboard.py' -WorkingDirectory $PWD -WindowStyle Hidden
```

If `running`: open the browser.

```bash
start http://localhost:5050
```

## Step 2 — DXLink Streamer

Check if the streamer is running:

```bash
python src/streamer.py --status
```

If `running` is `false`: start it as a hidden background process.

```bash
Start-Process python -ArgumentList 'src\streamer.py' -WorkingDirectory $PWD -WindowStyle Hidden
```

## Step 3 — Agent loop

Invoke the `/loop` skill with the prompt:

> Execute the next MEIC agent loop iteration following the operating instructions in CLAUDE.md.

Tell the user:
"Startup complete — dashboard at http://localhost:5050, agent loop started. The loop will self-pace each iteration."