Switch to a named risk profile (conservative/moderate/aggressive/very-aggressive), automatically backing up and updating `config.json`.

## Overview

Risk profiles bundle entry-gate thresholds with offsetting position-cap and stop-management constraints. Running `/set-risk-profile moderate` (for example) reads the named profile from `config.risk.json`, backs up your current `config.json`, overwrites the relevant keys, and reports what changed — **without restarting the loop**. The new settings take effect on the next iteration.

See [docs/risk-profiles.md](../docs/risk-profiles.md) for the full rationale, trade-offs, and when to use each profile.

## Step 1 — Check valid profile names

List available profiles:

```bash
python -c "import json; cfg = json.load(open('config.risk.json')); print('Available profiles:', ', '.join(cfg['profiles'].keys())); print('Current active profile:', cfg['active_profile'])"
```

Expected output:
```
Available profiles: conservative, moderate, aggressive, very-aggressive
Current active profile: conservative
```

If you get an error, check that `config.risk.json` exists in the project root and is valid JSON.

## Step 2 — Back up current config

Before making any changes, your current `config.json` is automatically backed up to `config.json.bak`:

```bash
copy config.json config.json.bak
```

(This happens automatically in Step 3's Python script; shown here for transparency.)

## Step 3 — Apply the profile

Replace `<profile_name>` with one of: `conservative`, `moderate`, `aggressive`, or `very-aggressive`.

```python
import json
import shutil
from pathlib import Path

# Load profiles and current config
with open('config.risk.json') as f:
    risk_profiles = json.load(f)

profile_name = '<profile_name>'  # e.g., 'moderate'

if profile_name not in risk_profiles['profiles']:
    print(f"ERROR: Profile '{profile_name}' not found.")
    print(f"Valid profiles: {', '.join(risk_profiles['profiles'].keys())}")
    exit(1)

profile = risk_profiles['profiles'][profile_name]
profile_note = profile.pop('_note', '(no description)')

# Back up current config
shutil.copy('config.json', 'config.json.bak')
print(f"✓ Backed up current config.json → config.json.bak")

# Load current config
with open('config.json') as f:
    current_config = json.load(f)

# Track changes for reporting
changes = {}
for key, value in profile.items():
    if key in current_config and current_config[key] != value:
        old_val = current_config[key]
        changes[key] = (old_val, value)
    current_config[key] = value

# Write updated config back
with open('config.json', 'w') as f:
    json.dump(current_config, f, indent=2)

# Update active_profile in config.risk.json
risk_profiles['active_profile'] = profile_name
with open('config.risk.json', 'w') as f:
    json.dump(risk_profiles, f, indent=2)

print(f"\n✓ Applied profile: {profile_name}")
print(f"\nProfile description:\n  {profile_note}\n")

if changes:
    print("Keys changed:")
    print("\n| Key | Old Value | New Value |")
    print("|---|---|---|")
    for key, (old, new) in sorted(changes.items()):
        print(f"| `{key}` | {old} | {new} |")
else:
    print("(No keys changed — already at this profile.)")

print(f"\n✓ Next loop iteration will pick up the new settings (no restart needed).")
print(f"✓ To revert, run: /set-risk-profile conservative")
```

## Step 4 — Verify the switch

Check that `config.json` and `config.risk.json` have been updated:

```bash
python -c "import json; cfg = json.load(open('config.json')); print('min_iv_rank:', cfg['min_iv_rank']); print('max_concurrent_ics:', cfg['max_concurrent_ics'])"
python -c "import json; cfg = json.load(open('config.risk.json')); print('Active profile:', cfg['active_profile'])"
```

Expected output (if you switched to `moderate`):
```
min_iv_rank: 0.22
max_concurrent_ics: 4
Active profile: moderate
```

## Step 5 — Revert if needed

To switch back to the previous profile:

```bash
copy config.json.bak config.json
```

Or to go back to `conservative` (the baseline):

```
/set-risk-profile conservative
```

## Summary

- **Switched to**: `<profile_name>`
- **Keys changed**: See the table above
- **When it takes effect**: Next loop iteration (~5 min)
- **To revert**: Run `/set-risk-profile conservative` or restore from `config.json.bak`
- **For details**: See [docs/risk-profiles.md](../docs/risk-profiles.md) for when to escalate, full trade-offs, and recommended progression

**Tip**: Check your loop log after the next iteration to confirm the new thresholds are active. Search for "entry_skip" reasons to see which gates are now most active.
