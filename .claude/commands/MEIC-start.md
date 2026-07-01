Start the full MEICAgent session: streamer and agent loop.

## Step 1 — DXLink Streamer

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
"Startup complete — agent loop started. The loop will self-pace each iteration."