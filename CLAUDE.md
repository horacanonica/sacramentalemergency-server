# Continuing this project

Start here: **[HANDOFF.md](HANDOFF.md)** (PDF twin: `HANDOFF.pdf`).

Pastor-facing reliability briefing: **[docs/RELIABILITY-FOR-PASTOR.md](docs/RELIABILITY-FOR-PASTOR.md)**.

That file is the current priest-facing + host guide. It is the source of truth for how the bot behaves. Read it before changing rotation, Signal menus, notifications, or availability.

**State as of 23 Sep 2026:** RingCentral upgraded this account to its new
call handling backend, which killed the legacy answering-rule API the app
wrote to. The driver was migrated to User Call Handling v2
(`RC_MODE=api-v2`) and verified against the live line — a commanded
switch was applied, read back, and restored. Automatic scheduling is
still **OFF** (it has been since 13 Aug 2026); the rotation goes live
when a priest texts `ENABLE`.

Same copy also lives at `~/Downloads/Sacramental-Emergency-Line-Rotation.md` (and `.pdf`).

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
- Coverage floor: at least one priest on the line; last remaining priest stays on despite a day off.
- No visit counting (removed 24 Sep 2026): no totals, no anointing log, no band-cross prompt. Rotation is `ROTATE` / dashboard only. Trip-report texts get a "no longer tracked" reply. Old count data is purged from state.json on load (`_REMOVED_COUNT_KEYS`).
- `STATUS` lists names only and marks off-line priests `(inactive)` right after the name.
- `AVAILABILITY` is Settings item 4, not a top-level command. Settings: 1 Add priest, 2 Remove priest, 3 View audit log, 4 Availability, 5 Set order (same `manual_override` as the dashboard; Y pushes RingCentral and texts everyone like ROTATE).
- Failsafe texts everyone (including muted). Routine broadcasts skip muted priests.
- Errors and a dead Signal daemon also SMS the priest cells via RingCentral from the emergency-line number, prefixed `Emergency Line bot:`.
- Do not put Tailscale IPs, dashboard passwords, or other-project paths in priest-facing docs.
- Hidden `EXECUTE ORDER 66` is not in HELP/ABOUT/HANDOFF. It toggles mute for the other priests and replies only to the sender.

## Run

```bash
docker compose up -d --build
```

Tests (from a venv with pytest): `pytest -q` — 199 passing as of 24 Sep 2026.

Check which RingCentral backend the account is on before debugging any
write failure:

```
GET /restapi/v1.0/account/~/extension/~/features?featureId=NewCallHandlingAndForwarding
```
