Set up MEICAgent credentials and verify the broker connection.

## Step 1 — Check current credential status

```bash
python src/tt.py secrets_status
```

Report which secrets are set and which are missing. The two required secrets are `client_secret` and `refresh_token`. `account_number` is optional (the SDK discovers accounts automatically if omitted).

## Step 2 — Set missing credentials

If any required secrets are missing, instruct the user to run this command **in their own terminal** (not here — getpass requires interactive input):

```bash
python src/tt.py secrets_set
```

This prompts for each credential with hidden input (no echo) and stores them in the OS keyring (Windows Credential Manager / macOS Keychain / Linux Secret Service). Existing values are preserved if the user presses Enter without typing.

To update a single key only:
```bash
python src/tt.py secrets_set --keys refresh_token
```

Tell the user: credentials are stored under service name `tastytrade-mcp` in the OS keyring, matching the layout used by tastytrade-mcp so existing secrets are reused automatically.

## Step 3 — Verify connection

After the user confirms credentials are set, test the broker connection:

```bash
python src/tt.py get_connection_status
```

A successful response includes `"ok": true` and account details. If it fails:
- `401 Unauthorized`: refresh token is expired — the user needs to re-authenticate via tastytrade and obtain a new refresh token
- `NoKeyringError`: no keyring backend — on Linux, install `python3-keyring` or `gnome-keyring`
- `CredentialError`: keyring read failure — check OS keyring permissions

## Step 4 — Verify account access

```bash
python src/tt.py get_account_info
```

Confirm buying power and NLV are visible. If the wrong account appears, set the explicit account number:

```bash
python src/tt.py secrets_set --keys account_number
```

## Step 5 — Check streamer (optional)

If the DXLink streamer is running, confirm it is healthy:

```bash
python src/streamer.py --status
```

If `running` is false and market hours are active, start it:

```bash
Start-Process python -ArgumentList 'src\streamer.py' -WorkingDirectory $PWD -WindowStyle Hidden
```

## Summary

Report:
- Which secrets are now set
- Whether `get_connection_status` succeeded
- Account number in use (masked to last 4 digits)
- Streamer status if checked
- Any remaining action items for the user
