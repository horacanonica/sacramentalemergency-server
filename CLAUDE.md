# Continuing this project

Start here: **[HANDOFF.md](HANDOFF.md)**.

Pastor-facing reliability briefing: **[docs/RELIABILITY-FOR-PASTOR.md](docs/RELIABILITY-FOR-PASTOR.md)**.

That file is the current priest-facing + host guide. It is the source of truth for how the bot behaves. Read it before changing rotation, Signal menus, notifications, or availability.

**State as of 23 Sep 2026:** RingCentral upgraded this account to its new
call handling backend, which killed the legacy answering-rule API the app
wrote to. The driver was migrated to User Call Handling v2
(`RC_MODE=api-v2`) and verified against the live line — a commanded
switch was applied, read back, and restored. Automatic scheduling has
been **ON** since 24 Sep 2026 (the failsafe turned it off at 8 PM on
29 Sep after a state-save race, since fixed; re-enabled the same night).

## What this repo is

Dockerized Flask + threads app: Signal bot (`signal-cli` JSON-RPC), RingCentral User Call Handling v2 API, California-timezone scheduler, password-protected dashboard.

- App code: `app/`
- Live state: `data/state.json`
- Roster / Signal allowlist: `config/priests.yaml`
- Tests: `tests/` (pytest; use a local venv, not the runtime image)
- Host ops (added 23 Sep 2026): `ops/` — nightly encrypted-USB backup,
  5-minute watchdog that texts priests (Signal via the app's daemon socket +
  RingCentral SMS), unattended-upgrades with midnight auto-reboot. Installed
  to `/usr/local/lib/sacline` + systemd timers by `sudo ops/install.sh`;
  re-run it after editing anything in `ops/`. Restore guide: `ops/RESTORE.md`.
- Power cuts (24 Sep 2026): `state.json` saves are fsynced and keep the
  previous version as `state.json.bak`; an unreadable `state.json` falls back
  to it at load and `app/main.py` alerts the priests (`recovered_from_damage`).
  `ops/boot_start.sh` (sacline-boot-start.service, every boot) starts the bot
  if it was left stopped and closes out a signal-cli update the restart cut
  off (puts the Dockerfile pin + image tag back if still on the old version).
  BIOS is set to power on when AC returns (tested 24 Sep 2026).
- signal-cli updates: `ops/signal_update_check.py` (Tue 10 AM) writes
  `data/signal-update-offer.json` only when a newer release exists (silent
  otherwise). `app/signal_update.py` asks unmuted priests Y/N (5 AM–9 PM, only
  those with no other pending question, so a rotate "Y" is never stolen) and
  writes `data/signal-update-response.json`. On "yes", `ops/signal_update.sh`
  backs up, bumps the Dockerfile pin, builds, tests, swaps, and rolls back
  image + Dockerfile + signal-cli-data on any failure. One writer per file.
- One-time welcome (24 Sep 2026): if `data/welcome-message.txt` exists when
  `EXECUTE ORDER 66` un-mutes the others, `app/welcome.py` sends it to them and
  deletes the file (a leading `DRAFT` line blocks sending; `#` lines are notes).
  With the file gone the hook is inert. Same pattern: if `data/order66-enable-automation`
  exists when Order 66 un-mutes, it also runs ENABLE, then deletes the flag.
- Tests: 11 tests in test_rotation/test_signal_bot fail when run after 8 PM
  California time (they use `california_today()`, the app uses the 8 PM
  handoff date). Pre-existing; run the suite before 8 PM for a clean result.

- Troubleshooting (added 27 Sep 2026): Signal `TROUBLESHOOT` wizard
  (`app/troubleshoot.py`) on top of `app/doctor.py` (checks, fixes, masked
  report, monthly digest, review packet; also a CLI: `python -m app.doctor
  checks|report|review|resend`). Server menu `ops/sacline` (installed to
  /usr/local/bin). Restarts requested over Signal go through
  `data/host-request.json` → `sacline-host-action.path` → `ops/host_action.sh`
  → `data/host-request-result.json`. Operations journal
  `data/ops-journal.jsonl` (`app/ops_journal.py`; host side
  `ops/journal_append.py`) records every notable event for the 3-month review
  (`docs/OPERATIONS-REVIEW.md`, due early Jan 2027). Guides:
  `docs/TROUBLESHOOTING.md`, `docs/HANDOFF-CHECKLIST.md`. `AGENTS.md` and
  `GEMINI.md` point other AI assistants here; keep them in sync.

## Rules that must stay true

- Signal Messenger only. Roster cell numbers are the allowlist.
- signal-cli runs with `--trust-new-identities always` (see
  `app/signal_client.py`). Without it, a priest getting a new phone
  silently breaks send/receive to them until a human manually runs
  `trust` - discovered 23 Sep 2026 with Fr James Martin SJ, nobody was around to
  fix it. Do not revert this to the default (`on-first-use`): our own
  access control is the roster check in `config/priests.yaml`, done
  independently at the app layer - an unrecognized number is declined
  there regardless of Signal-level trust, so this flag isn't loosening
  the actual security boundary.
- RingCentral writes go through `RC_MODE=api-v2` (`CommHandlingApiDriver`):
  `PATCH .../comm-handling/voice/state-rules/work-hours`, reordering the
  `RingGroupAction` entries in `dispatching.actions`. Never write any other
  state rule, greeting, or voicemail; never drop the `TerminatingAction`.
- The RingCentral `auth` rate-limit group allows **5 token exchanges per
  60 seconds**. The driver caches its token — do not revert that. A 429
  reads to this app as a rejected write and escalates to a failsafe that
  texts every priest.
- Every ring write is read back and verified. "Accepted but not applied"
  is a failure, not a rotation.
- California time, hardcoded. Absences run 8:00 PM the evening before through 8:00 PM on the listed day.
- Days off, vacations, retreats and absences only toggle a priest's RingCentral leg **off** (`enabled: false`, kept in the list below the ringing legs); `apply_order` never deletes a leg. The bot never deletes a priest on its own. Deletion comes only from a person: Signal Settings > 2 Remove priest (after the "will be deleted from the system … Confirm? Y/N" warning; it also deletes his leg via `delete_leg`), the dashboard remove (also deletes the leg), or a leg deleted by hand in RingCentral (adopted at 8 PM, below). `apply_order` never re-creates a leg someone deleted by hand; it only creates legs for priests added through the bot (`rc_new`). (Rule set 27 Sep 2026 after the first automatic switch deleted Fr. Lopez's leg.)
- Deleted priests are kept 30 days (`deleted_priests` in state.json, `DELETED_KEEP_DAYS`) with their schedule; Settings > 3 Restore recently deleted (listed by name and number) brings one back, and so does re-adding his number by hand in RingCentral. After 30 days the record is purged (`deleted_priest_purged` in history).
- 8 PM check (`app/rc_sync.py`, added 27 Sep 2026): once per ring day, in `_sync_automatic_ring` just before tomorrow's ring is written, the bot reads the whole Ring in order list and compares it with what it last wrote (`last_applied_order` + roster). RingCentral is taken as correct: unknown numbers join the roster/allowlist, deleted legs leave it, a leg switched off by hand sets `manual_disabled` (switched on clears it), and a hand-changed order is adopted. Changes are texted to the unmuted priests; no change is only a `rc_check` history entry. Numbers not on the roster are still switched off by any write before 8 PM.
- Unknown number texts the bot: before the "isn't authorized" reply, `_admit_if_on_ring` (signal_bot) reads the RingCentral ring list; if his number is on it he is admitted right away (`rc_sync.admit_ring_leg`, restores a 30-day-deleted record if there is one), the other priests are told, and he gets the welcome setup (`app/onboarding.py`, pending `welcome_setup`: day off → recollection → vacation, SKIP / SKIP ALL, 1-hour timeout). A restored priest gets "Welcome back" instead. Misses are not looked up again for 10 minutes. The 8 PM check sends the same welcome.
- Standing weekday rule (`ring_last_on: [Monday]` on a priest in `config/priests.yaml`, added 1 Oct 2026): `RotationManager._apply_ring_last` (in `effective_order`) moves him to the end of the live ring on that ring day when he would otherwise be first. Also applies (any priest, no config) on a day that is nominally his day off but was skipped (week skip, or a peer's vacation cancelled it; `_day_off_skipped`, automation on only, added 12 Oct 2026): if he would ring first he rings last. Live ring only; saved order untouched. `adopt_live_ring` skips a live ring equal to `effective_order()` so the Monday audit / 8 PM check never absorb the shift into the saved order. Read from the config file on each call, but restart the bot to be safe after editing.
- One-time moves (7 Oct 2026): changing an existing day off or recollection through the Signal menu or the dashboard offers three choices: **skip this week's (this month's) one, move just this week/month, or move permanently**. Permanent = `set_day_off` / `set_day_of_recollection` (these also wipe that priest's pending one-time moves, never his skips). One-time = `move_day_off_this_week` (existing `week_day_off_overrides` "move") / `move_recollection_this_month` (new `recollection_month_overrides` `{"YYYY-MM": {priest_id: ordinal}}`, replaces the standing ordinal for that month only; old months are pruned on write). "This week/month" means the week/month of his NEXT occurrence, so once this week's day off (or this month's recollection) has passed the move goes to the next one: never two in one period. A target date already past, a missing 5th Wednesday, or a move that empties the line is refused. Skip = `scheduled_skip_targets` + `skip_scheduled_absence`. Undo = `skipped_targets` + `unskip_scheduled_absence` (Availability `UNSKIP`, pending `menu_unskip_which`, added 12 Oct 2026; removes the week override / `recollection_skips` entry). Signal pending types `menu_day_off_scope` / `menu_recollection_scope`. Tests: `tests/test_moves.py` (clock frozen).
- "Finished with ...? Y/N" (7 Oct 2026): after a Settings or Availability reply that SAVED something and would have ended the menu, `_ask_if_finished` (signal_bot) asks `Finished with availability? Y/N` / `Finished with Settings? Y/N` (pending `menu_finished`); N returns to that menu (same priest for Availability), Y ends it, SETTINGS opens Settings. "Saved" = `RotationManager.change_count` (counts `_log` calls, not persisted) moved during the reply, or N to "Add another trip?". Nothing is asked after a cancel, an error, or View audit log. New menu handlers need no extra code, but a new pending type must be added to `_AVAILABILITY_MENU_TYPES` / `_SETTINGS_MENU_TYPES`.
- Set order (Settings 6, 12 Oct 2026): after the new order is entered the bot asks **1 today only / 2 permanent**. Permanent = `manual_override` (saved order). Today only = `RotationManager.set_ring_override_today` (`ring_override` in state.json `{day, order}`): `effective_order` sorts the live ring by it while `effective_ring_date()` matches, so it lapses at 8 PM by itself; saved order untouched, and the ring-last rules are skipped while it is active. ROTATE (`_handle_rotate`) and a permanent order clear it (`clear_ring_override`). The prompt (`_set_order_prompt`) lists the saved order and, when any rule changes the ring, the live ring (`live_ring_view`) listed with the SAME saved-order numbers (one set of numbers; replies use them).
- Shortcuts (12 Oct 2026): `FIRST <name>` (`_handle_first`, pending `first_confirm` Y/N, then `_apply_new_order(today_only=True)`, the same path as Set order; refuses a priest off the line); top-level `SKIP` / `UNSKIP` reuse `_handle_menu_availability` for the sender; `AWAY MM/DD-MM/DD` / `AWAY` (`_handle_away_shortcut`) adds a trip with no "Add another?" question. The "Finished with ...?" question is now asked only after Availability changes, never after Settings.
- Coverage floor: at least one priest on the line; last remaining priest stays on despite a day off.
- No visit counting (removed 24 Sep 2026): no totals, no anointing log, no band-cross prompt. Rotation is `ROTATE` / dashboard only. Trip-report texts get a "no longer tracked" reply. Old count data is purged from state.json on load (`_REMOVED_COUNT_KEYS`).
- `STATUS` lists names only and shows the LIVE ring (12 Oct 2026): `rc_sync.live_ring_view` reads RingCentral (`read_order`) and `RotationManager.live_view` puts ringing priests first in that order, then off-line priests marked `(inactive: <reason>)` right after the name (`RotationManager.off_reason`: disabled, vacation, day of recollection or day off; plain `(inactive)` when no rule explains it, e.g. switched off by hand in RingCentral; 14 Oct 2026). The Set order screen's live list uses the same tag. The Day off section marks skipped days off, e.g. `Fr. X: Monday (skipped 10/12)` (`RotationManager._skipped_day_offs`); falls back to `effective_order()` if RingCentral cannot be read. The dashboard (`app/web/__init__.py`, mode 444) still lists the saved order.
- `AVAILABILITY` is Settings item 5, not a top-level command. Settings (renumbered 27 Sep 2026): 1 Add priest, 2 Remove priest, 3 Restore recently deleted, 4 View audit log, 5 Availability, 6 Set order (same `manual_override` as the dashboard; Y pushes RingCentral and texts everyone like ROTATE).
- Failsafe texts everyone (including muted). Routine broadcasts skip muted priests.
- Errors and a dead Signal daemon also SMS the priest cells via RingCentral from the emergency-line number, prefixed `Emergency Line bot:`.
- Do not put Tailscale IPs, dashboard passwords, or other-project paths in priest-facing docs.
- Call log (`app/call_log.py`, added 27 Sep 2026): every minute the scheduler reads new
  incoming calls from RingCentral's call log (read-only). `data/call-log.jsonl` keeps only
  the caller's LAST 4 DIGITS plus an anonymous HMAC fingerprint (`data/call-fp.key`);
  `data/calls-recent.jsonl` has full numbers and is pruned to 24 hours. Never put full
  numbers anywhere else (journal, reports, digests, git). A call with no pickup and no
  voicemail (RingCentral result Missed/Blocked/etc.) is NEVER treated as spam: the
  priests on the line (everyone if nobody is) get an immediate Signal text, any hour,
  muted or not, SMS fallback if Signal is down; repeat calls each get their own text.
  The first sync imports the year to date silently. Short "Call connected" FindMe legs
  (< 40 s) on missed calls are the priest's own phone voicemail, not a pickup.
  Callers reach Ext. 1 (the only extension this app touches) from the main parish
  number (MainCompanyNumber) by pressing 1, or a secretary answers the main number
  and transfers them: the TransferCall leg names that staff member (`via`), the call
  did NOT go out to staff (verified in the account-wide call log, 27 Sep 2026). Staff
  calling Ext. 1 internally show an extensionNumber and no phone number (`internal`).
  Former priests are named from `config/former-priests.yaml` (last 4 → name; not an
  allowlist; git-ignored); regular callers from `config/known-callers.yaml` (last 4 →
  label, e.g. a hospital's chaplains); staff who left are shown by job title
  via `config/staff-titles.yaml` (old RingCentral name → title). After editing these,
  re-import to relabel old calls: move `data/call-log.jsonl` and `data/call-sync.json`
  aside; the next sync imports the year again without alerts.
  `CALLS` (last 3 calls in 24 h, full numbers; `call_log.history_text`) / `CALLS ALL` (every call in 24 h) / `CALLS <days>` / `CALLS REPORT`; `HISTORY` is a hidden alias of `CALLS` in Signal; `sacline` → c.
- Days as #1 (`app/ring_history.py`): replays RingCentral's audit trail of Ext. 1 ring
  changes (order, on/off, add/remove; kept ~6 months by RingCentral, copied hourly to
  `data/ring-history.jsonl` and kept for good). Before the audit trail starts (1 Apr
  2026) it is estimated from each call's first-rung phone. Every stats text reports
  how many calls agree with the timeline (113/113 when built, 28 Sep 2026).
- Troubleshooting stays safe: the Signal wizard offers nothing destructive (no backup restores,
  no code rollbacks; those are `sacline` only, with confirmation). Every fix is journaled. Reports
  go through `doctor.mask()` (phones to last 4 digits, IPs and tokens removed) and never read `.env`.
  New notable events get a `journal(...)` line so the review sees them.
- Hidden `EXECUTE ORDER 66` is not in HELP/ABOUT/HANDOFF. It toggles mute for the other priests and replies only to the sender.

## Run

```bash
docker compose up -d --build
```

Tests (from a venv with pytest): `pytest -q` — 315 passing as of 7 Oct 2026 (run before 8 PM, or with time-machine set to noon).

Check which RingCentral backend the account is on before debugging any
write failure:

```
GET /restapi/v1.0/account/~/extension/~/features?featureId=NewCallHandlingAndForwarding
```
