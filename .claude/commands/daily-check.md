Run the MEICAgent daily connection check. Executes once per trading day at the start of the first market-hours iteration.

1. Check whether the daily connection check has already run today:
```bash
python src/db.py get_session_init
```
If `already_run` is `true`, skip the remaining steps and return immediately.

2. Verify the broker connection is live:
```bash
python src/tt.py get_connection_status
```

3. Log the result and mark the check as complete:
```bash
python src/notify.py log_event --level INFO --message "Daily connection check: <connected|failed>" --data '{"connected": true|false}'
python src/db.py set_session_init
```

If the connection is not live, log a WARN and do not proceed with market assessment or entry decisions this iteration.
