Display the current MEICAgent session status as a clear, readable summary.

Run the following in parallel:
```bash
python src/db.py get_open_trades
python src/db.py get_today_count
python src/db.py get_today_pnl
```

Also query the last 5 loop log entries:
```bash
python -c "
import sqlite3, json
conn = sqlite3.connect('data/meic_trades.db')
conn.row_factory = sqlite3.Row
rows = conn.execute('SELECT loop_time, action, reasoning, iv_rank, underlying_price, session_quality FROM loop_log ORDER BY loop_time DESC LIMIT 5').fetchall()
print(json.dumps([dict(r) for r in rows], default=str))
conn.close()
"
```

Present the results as a concise status report:

- **Open positions**: for each open/partial trade, show order ID, strikes, net credit, current stop levels, status, and time entered
- **Today's summary**: total entries, net P&L to date
- **Last loop action**: what the agent did on its most recent iteration and when
- **Recent loop history**: brief summary of the last 5 actions
- **Any active conflicts or warnings**: flag if the most recent loop logged a WARN

Keep the output brief and scannable — this is a quick check, not a full report.
