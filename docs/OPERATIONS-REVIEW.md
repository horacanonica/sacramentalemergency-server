# Operations review

**Why:** the TROUBLESHOOT wizard and `sacline` menu cover problems we could foresee. After three
months of real use, the operations journal shows which problems actually happen. The review turns
those into specific fixes, before the server is handed off.

**Recording started:** 27 Sep 2026. **First review due:** first week of January 2027
(with the admin and Claude Code, before hand-off).

## What is recorded automatically

`data/ops-journal.jsonl` gets one line per event, and is kept for 12 months and backed up nightly:

| Kind | Event |
|---|---|
| `rc_write` / `rc_write_failed` | every ring change sent to RingCentral, and every failure |
| `rc_check` / `rc_hand_edits` / `rc_check_failed` | the nightly 8 PM check ("no change", hand edits adopted, or RingCentral unreachable) |
| `failsafe`, `automation_on` / `automation_off` | switching turned off by a failure, and ENABLE / DISABLE |
| `audit_pass` / `audit_fail` | Monday 9 AM self-audit |
| `signal_down` / `signal_up` | Signal stopped or recovered |
| `bot_started`, `state_recovered` | restarts, and saved-data recovery after a power cut |
| `troubleshoot`, `doctor_fix`, `host_request`, `host_action` | wizard runs and every fix applied |
| `watchdog_alert` / `watchdog_recovered` (host) | server problems: container down, disk, backups, updates |
| `signal_update` (host) | signal-cli update progress |
| `monthly_digest` | the monthly summary was sent |

On the 1st of each month at 9 AM, the admin (the priest marked `audit_notices` in
`config/priests.yaml`) gets a Signal summary of the previous month.

## How to run the review

1. On the server: `sudo sacline` → **8** (90 days). This saves `data/ops-review-YYYY-MM-DD.md`.
2. Start Claude Code (or another assistant) in the project folder and say:

   > Read CLAUDE.md, docs/OPERATIONS-REVIEW.md and data/ops-review-….md. For every incident type,
   > explain what happened and what fixed it. Then propose new TROUBLESHOOT options (app/troubleshoot.py),
   > checks/fixes (app/doctor.py) and sacline menu items (ops/sacline) so the priests can fix these
   > themselves next time. Ask before building.

3. Record the findings below. Update docs/TROUBLESHOOTING.md for any new options.

## Findings

*(Filled in at each review.)*

| Review | Period | Incidents seen | What was added |
|---|---|---|---|
| | | | |
