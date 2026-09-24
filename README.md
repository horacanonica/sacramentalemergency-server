# Sacramental Emergency Line Rotation

**Continuing this work (Claude or anyone else):** read [`HANDOFF.md`](HANDOFF.md) first. Same guide as a PDF: [`HANDOFF.pdf`](HANDOFF.pdf).

Automates rotating the priest ring order on the parish's Sacramental
Emergency Line, pushing each change to RingCentral directly.

**If you are not a programmer and something is broken, skip to
["Something's wrong" below](#somethings-wrong-a-decision-tree).**

## What this does

- Keeps track of which priest is first, second, third in the phone
  rotation, and how many calls the lead priest has taken.
- Lets any of the three priests text a Signal number to trigger a
  rotation (whoever's first moves to last).
- Sends a confirmation text to all three priests whenever the order
  changes.
- Has a web page (password protected) for the same thing plus a
  history log, manual override, and adding/removing/swapping priests.
- Pushes the new order to RingCentral directly (`RC_MODE=api-v2`), and
  reads it back to confirm RingCentral actually applied it. In
  `RC_MODE=manual` it still does everything above, but a human has to
  make the change by hand in the RingCentral Admin Portal (the app will
  remind you every time).

## Requirements

- Docker + Docker Compose (this is how it's meant to run — see
  `docker-compose.yml`). Everything else is pinned inside the container.
- A dedicated phone number for the Signal bot (a Google Voice number,
  per the original plan) that you can register signal-cli to.
- RingCentral API credentials if/when `RC_MODE=api` (see `.env.example`).

## First-time setup

1. Copy `.env.example` to `.env` and fill in real values. At minimum
   set `WEB_ADMIN_USERNAME`/`WEB_ADMIN_PASSWORD` and
   `FLASK_SECRET_KEY` (any random string) before exposing this on any
   network.
2. Copy `config/priests.example.yaml` to `config/priests.yaml` and fill in
   each priest's real `cell_number` (E.164 format, e.g. `+19165551234`).
   That file is git-ignored: it holds real phone numbers.
3. Build and start: `docker compose up -d --build`
4. **Register signal-cli** (one-time, interactive — must be run on the
   actual machine that will host this long-term; see the portability
   note below if you built this on a different machine):
   ```
   docker compose exec rotation-app signal-cli -a <BOT_NUMBER> register
   # you'll get a verification code by SMS
   docker compose exec rotation-app signal-cli -a <BOT_NUMBER> verify <CODE>
   ```
5. Visit `http://<host>:8420` and log in with the admin credentials
   from step 1.
6. Text `STATUS` to the bot number from one of the priests' phones to
   confirm the Signal side works.

## Built on a Mac, deployed on Linux

This was originally developed on a MacBook Pro but is meant to run
long-term on a Linux machine (Linux Mint, per the original plan).
Because everything runs inside Docker, moving it is meant to be:

1. Copy the whole project folder to the Linux machine (or `git clone`
   it there).
2. Copy `.env` and `config/priests.yaml` over too (`.env` is
   git-ignored on purpose - move it manually).
3. Copy the `data/` and `signal-cli-data/` folders if you want rotation
   history and the existing Signal registration to carry over.
   **If you don't copy `signal-cli-data/`, you must re-run the
   signal-cli `register`/`verify` steps on the Linux machine** -
   registration is tied to whichever machine last linked the number.
4. `docker compose up -d --build` on the Linux machine.

No code changes are needed for the move - Python and signal-cli both
run identically on macOS and Linux, and Docker is what makes "identical"
actually true instead of just probably true.

## Rotation logic

Lives entirely in `app/rotation.py`, with zero dependency on
RingCentral, Signal, or the web framework - see the tests in
`tests/test_rotation.py` for the exact behavior. In short: rotating
moves the first priest to the last position. It happens only when a
priest texts `ROTATE` or someone presses "Rotate now" on the dashboard.
Visits are not counted (removed 24 Sep 2026), so `ROTATION_CALL_THRESHOLD`
in `.env` is no longer used.

## RingCentral API modes

This account is on RingCentral's **new** call handling backend
(`NewCallHandlingAndForwarding`), so it runs `RC_MODE=api-v2`.

| `RC_MODE` | Driver | Use when |
|---|---|---|
| `api-v2` | `CommHandlingApiDriver` — User Call Handling v2 | Account upgraded to the new backend (this one, since Sept 2026) |
| `api` | `AnsweringRulesApiDriver` — legacy answering-rule v1 | Account not yet upgraded |
| `manual` | `ManualModeDriver` — no writes at all | Credentials missing, or you want the app to only track and remind |

To check which backend an account is on:

```
GET /restapi/v1.0/account/~/extension/~/features?featureId=NewCallHandlingAndForwarding
```

`isNewBackendAvailable: true` means the v1 `answering-rule` endpoints
return `403 CMN-468` for that extension and you must use `api-v2`.

What `api-v2` writes: the ring order on the **business-hours state
rule** (`work-hours`) only — it reorders the `RingGroupAction` entries
inside `dispatching.actions` and passes everything else through
untouched. It does not write voicemail, greetings, the emergency number
itself, or any other state rule, and it refuses to write a call flow
that has lost its `VoiceMailTerminatingTarget`.

On this extension `work-hours` is scheduled 00:00–23:59 daily, so there
is no separate `after-hours` rule and the bot's order governs nights as
well as days.

**Rate limits.** RingCentral's `auth` group allows only **5 token
exchanges per 60 seconds**. The driver caches its access token for the
token's lifetime and retries once on a 429, honouring `Retry-After`.
Do not "simplify" that back into a per-request token exchange: the
Monday audit makes several switches back to back, and a 429 there
reads to this app as a rejected write, which escalates into a failsafe
that texts every priest.

## Something's wrong: a decision tree

**A priest didn't get a confirmation text after rotating.**
→ Check the dashboard's History section - did the rotation log at all?
  - No: the rotation itself failed. Check `docker compose logs rotation-app`
    for a traceback near the time in question.
  - Yes: the rotation worked but notification failed. Check if an email
    alert arrived instead (email is the fallback when Signal fails).
    If neither arrived, Signal itself is probably down - see below.

**Signal isn't sending or receiving anything.**
→ `docker compose exec rotation-app signal-cli -a <BOT_NUMBER> receive`
  — if this errors, signal-cli's registration may have expired or the
  container's `signal-cli-data` volume got lost. Re-run the registration
  steps in "First-time setup" step 4.

**The web dashboard won't load.**
→ `docker compose ps` - is `rotation-app` running? If not,
  `docker compose logs rotation-app` for the crash reason, most likely
  a missing/misspelled value in `.env`.

**Callers say the ring order is wrong even though the dashboard shows
the right order.**
→ Check `RC_MODE` in `.env`. In `manual` mode the app's tracked order
  and RingCentral's actual order are two different things, and the
  Portal has to be updated by hand - see the banner on the dashboard.
  In `api-v2` mode a mismatch should be impossible (every write is read
  back and verified), so check `docker compose logs rotation-app` for a
  failed push instead.

**Still stuck / something not covered here.**
→ Copy the relevant lines from `docker compose logs rotation-app`
  and paste them, along with what you were trying to do, to Claude
  (claude.ai) with this prompt:

  > I run a small Docker-based rotation app for a parish phone
  > system (Python/Flask, signal-cli for texting, optionally
  > RingCentral's API). Here's what I was trying to do: [describe].
  > Here's the relevant log output: [paste]. What's likely wrong and
  > what should I check next?

  Attaching this repo's `README.md` and the relevant file from `app/`
  will help Claude give a more specific answer.

## Long-term maintenance notes

- The Docker base image and signal-cli version are pinned on purpose
  (see top of `Dockerfile`). Don't switch to `latest` tags - bump the
  pinned versions deliberately, ideally with someone available to fix
  fallout if a bump changes behavior.
- RingCentral's core API is expected to stay stable for a few years at
  a time; if things start failing after a long period of no changes,
  check RingCentral's developer changelog/status page first before
  assuming this code broke on its own.
- `config/priests.yaml` and `.env` are the only files a future
  non-technical admin should need to touch by hand; everything else is
  reachable through the web dashboard.
