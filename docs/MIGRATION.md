# Moving this project to a new computer

Checklist for relocating the Sacramental Emergency Line rotation app off
this machine. Most of this stems from decisions made while first standing
this up here — see the inline "why" on each item.

## Before you leave the old machine

- [ ] **Stop the container cleanly**: `docker compose down` (not `docker
      compose kill`) so signal-cli's daemon and Flask shut down gracefully
      rather than getting hard-killed mid-write.
- [ ] **Copy the whole project folder**: `/home/hal9000/sacramental-line-rotation/`
      in full, including the dotfiles (`.env` is git-ignored on purpose, so
      a plain file copy or `scp`/`rsync` — not `git clone` — is what
      actually carries it over). [`package-for-move.sh`](package-for-move.sh)
      builds a single verified archive of exactly what needs to travel —
      run it from the project root when you're ready to actually move (not
      before; it captures a snapshot, and rotation state changes daily):
      ```bash
      ./docs/package-for-move.sh
      ```
      It checks that `.env`, `data/`, `signal-cli-data/`, and
      `config/priests.yaml` all made it in, and prints the resulting file's
      size and checksum. Move that one file over with `scp`/`rsync` — it
      contains real secrets (RingCentral credentials, dashboard password,
      priests' cell numbers), so treat it the same as `.env` itself: never
      email it, upload it anywhere, or leave a copy sitting around longer
      than the move takes.
- [ ] Specifically confirm these directories came with it (easy to miss
      since they're git-ignored / created at runtime):
  - [ ] `data/` — rotation state and history log
  - [ ] `signal-cli-data/` — the bot's Signal registration. **Do not
        register a fresh number on the new machine if this comes over
        intact** — you'd be creating a second, different bot account.
  - [ ] `config/priests.yaml` — has the priests' real cell numbers filled
        in; not in `.env.example`-style templates.

## Do not run both machines at once

- [ ] Once you `docker compose up` on the new machine, do not also leave
      the old machine's container running against the same
      `signal-cli-data/`. Two live daemons on the same Signal account at
      once can cause session/key conflicts. Stop the old one first,
      confirm it's down, then start the new one.

## Things that must change in `.env` on the new machine

- [ ] **`TAILSCALE_IP`** — this is the one guaranteed to break the move if
      skipped. `docker-compose.yml`'s port binding reads this from `.env`
      to keep the dashboard off the public internet. Run `tailscale ip -4`
      **on the new machine** and update this value, or `docker compose up`
      will fail immediately with "set TAILSCALE_IP in .env" / fail to bind
      the port.
- [ ] Everything else in `.env` (RingCentral creds, `SIGNAL_BOT_NUMBER`,
      `WEB_ADMIN_USERNAME`/`PASSWORD`, `FLASK_SECRET_KEY`,
      `ROTATION_CALL_THRESHOLD`, etc.) is host-independent and should
      carry over unchanged.

## Hardware: what this actually needs

Measured on hal9000, 23 Sep 2026, container idle and healthy:

| Resource | Actual use | Comment |
|---|---|---|
| RAM | **259 MB** | One Python process + one `signal-cli` JVM. The JVM is the bulk of it. |
| CPU | **0.07%** | A 60-second scheduler tick and a 3-second Signal poll. Idle almost always. |
| Disk (image) | **783 MB** | Rebuilt from the Dockerfile on the new machine. |
| Disk (state) | **8.3 MB** | `data/` + `signal-cli-data/`, and it grows very slowly. |

So the bar is low: **any x86-64 box with 4 GB RAM and ~20 GB of free
disk is comfortable.** Add the Docker daemon and a headless Linux and
you are still under 1 GB of RAM in use. A fanless Celeron/N-series mini
PC (N4000/N4020/N100 class) is more than enough — the workload is
overwhelmingly idle, and its one CPU-bound moment is JVM startup.

Two things matter more than the spec sheet:

- [ ] **Run the OS from an SSD, not eMMC, if the machine offers the
      choice.** Many of these mini PCs ship with 64 GB of soldered eMMC
      plus an empty SATA/M.2 slot. eMMC is the least reliable part of a
      box that is meant to sit untouched for years, and this one runs an
      emergency line. Put a small SSD in the slot and install there.
- [ ] **Wired Ethernet, not Wi-Fi.** A dropped Wi-Fi link means the
      Signal bot stops receiving commands. The line keeps ringing, but
      the priests lose the ability to text it.

Also worth doing on a dedicated box:

- [ ] **Set the BIOS to power on automatically after power loss**
      (often "Restore on AC/Power Loss" → "Power On"). Otherwise the
      machine stays off after an outage until someone notices.
- [ ] **Put it on a small UPS.** `restart: unless-stopped` brings the
      container back after a reboot, but only once the machine boots.
- [ ] **ARM boxes need extra checking** — see the CPU architecture note
      below. An x86-64 mini PC avoids that entirely.

**What happens if this machine dies:** RingCentral keeps ringing the
last order it was given. Callers still reach a priest. What stops is
automatic reordering and the Signal bot. That is the whole reason a
modest, boring, single-purpose box is an acceptable host — see
`RELIABILITY-FOR-PASTOR.md`.

**Why moving off hal9000 is a real improvement:** on hal9000 this app
shares a machine (and a Docker daemon) with Plex, Nextcloud, Immich and
several other stacks. A dedicated box means an unrelated rebuild,
disk-full, or reboot elsewhere can no longer take the emergency line's
automation down with it.

## Docker vs. installing natively

**Keep Docker.** Measured on hal9000 and scaled down to what a single-app
box actually needs (see below), the answer isn't close.

On Linux, Docker isn't a VM — no hypervisor tax, containers are just
namespaces/cgroups on the host kernel. The only real cost is the daemon
itself: measured on hal9000 (which runs 16 containers, so the raw number
there is inflated), the fixed overhead is `dockerd` (~131 MB) +
`containerd` (~88 MB), plus about 17 MB per running container
(`containerd-shim`) and 9 MB per published port (`docker-proxy`). Scaled
to a dedicated box running just this one container with its one port:

| | RAM | Disk |
|---|---|---|
| Docker engine (daemon + 1 shim + 1 proxy) | ~245 MB | ~350 MB |
| This app's container | ~260-320 MB | ~783 MB (the image) |
| **Total** | **~560 MB** | **~1.1 GB** |

Against 4 GB RAM and the ~20 GB disk budget above, that leaves ~3.5 GB
RAM free on a box that's otherwise idle. Going native only claws back
the ~245 MB engine overhead — meaningless headroom on hardware that runs
nothing else.

Beyond the resource math, Docker is also the safer choice for stability
specifically on this project:

- `restart: unless-stopped` already gives crash/reboot recovery, tested.
  A native install needs an equivalent systemd unit written and tested
  from scratch — new code on the machine you most want to avoid
  introducing new failure modes on.
- The Dockerfile pins the exact JRE (Temurin) and signal-cli versions
  deliberately, isolated from the host. A native install still needs the
  same Adoptium apt repo (Debian's own JRE tops out at 17; signal-cli
  needs 25) — that complexity doesn't disappear, Docker just contains it
  in a rebuildable image instead of mutating host state.
- `new-machine-bootstrap.sh` above turns on `unattended-upgrades` for
  the host OS. With Docker, that only ever touches the OS — the app's
  runtime stays exactly as pinned until someone deliberately edits the
  Dockerfile. Without Docker, unattended-upgrades would eventually touch
  the same Python/JRE the app runs on, undermining the "pin deliberately,
  bump on purpose" rule the Dockerfile itself states as its rationale.
- Every doc in this repo (`CLAUDE.md`, `HANDOFF.md`, this file) and the
  bootstrap script above already assume Docker, and it's the exact
  configuration tested live against RingCentral on 23 Sep 2026.

## Choosing and installing the OS

**Recommendation: Debian 13 ("trixie"), the current stable release,
installed headless.** hal9000 itself already runs Debian 13 with Docker
29.8.1 and Tailscale 1.102.4 — both work cleanly there today, so this
isn't a guess, it's already proven on the exact software this app needs.
Staying on the current stable release also buys the longest run before
the box needs a disruptive OS upgrade, which matters for a machine meant
to sit untouched for years.

Debian 12 ("bookworm") is a fine second choice if trixie feels too new —
everything here runs inside Docker, so the host OS mostly just needs to
keep Docker, Tailscale, and SSH working. Either release does that.

What to skip and why:

- **Ubuntu Server** — no real advantage here. snapd and cloud-init exist
  to solve cloud-fleet problems this single appliance doesn't have, and
  it'd be one more unfamiliar thing next to a Debian box (hal9000) you
  already know.
- **Alpine / other musl-based distros** — the app's own container
  already carries its glibc dependencies (Temurin JRE, signal-cli), so
  Docker isolates that either way, but a musl host is one more unfamiliar
  edge case for zero benefit.
- **DietPi and other appliance distros** — they're Debian underneath
  anyway. Their extra tooling (SBC power tweaks, bundled dashboards) is
  aimed at problems this project doesn't have.

**Installer settings that matter:**

- [ ] Use the standard Debian installer (not netinst if you're offline
      during setup — the full installer image avoids needing internet
      access mid-install; either works fine once the box has Ethernet).
- [ ] At the software-selection ("tasksel") screen, check **only** "SSH
      server" and "standard system utilities." Uncheck every desktop
      environment. This alone gets the install down to roughly 1-2 GB
      and keeps boot near-instant — appropriate for a box that is
      otherwise idle almost 100% of the time (see measurements above).
- [ ] Accept the installer's default of including **non-free firmware**
      (this has been the default since Debian 12, specifically because
      cheap mini PCs like this one often need a non-free blob for their
      Wi-Fi/Bluetooth chipset). You plan to run wired Ethernet, so you
      likely won't need it — but it's one less thing to debug if you
      ever have to bring the box up before Ethernet is plugged in.
- [ ] Set up a non-root user with sudo during install; you'll want that
      for the bootstrap script below and for day-to-day SSH access.
- [ ] Enable the SSH server during install (or `apt install openssh-server`
      after) so you never need a monitor/keyboard on this box again.

**Once Debian is installed and reachable over SSH**, run
[`new-machine-bootstrap.sh`](new-machine-bootstrap.sh) to install Docker,
Tailscale, and unattended security upgrades in one pass — see the
"New-machine prerequisites" section below for how it fits into the rest
of this checklist. Copy it over however is convenient (it doesn't depend
on the rest of the project folder):

```bash
scp docs/new-machine-bootstrap.sh youruser@<new-machine-ip>:~/
ssh youruser@<new-machine-ip> 'chmod +x new-machine-bootstrap.sh && ./new-machine-bootstrap.sh'
```

It's a plain shell script — worth reading before you run it, same as any
script that asks for sudo. It stops short of `tailscale up` (that step
needs an interactive browser login) and prints what to do next.

## New-machine prerequisites

- [ ] Docker (with the Compose v2 plugin — `docker compose version`,
      not the standalone `docker-compose`) and Tailscale installed. Run
      `new-machine-bootstrap.sh` (see above) to get both plus
      unattended-upgrades in one pass, or do it by hand; either way,
      `docker-compose.yml`'s `${TAILSCALE_IP:?...}` required-var syntax
      needs Compose V2, and Compose V1 will fail on it with a confusing
      error.
- [ ] `tailscale up` and authenticate on the new machine, joined to the
      same tailnet, before you try to reach the dashboard remotely. The
      bootstrap script installs the `tailscale` package but does not run
      this — it needs an interactive login.
- [ ] **CPU architecture**: the Dockerfile pins `signal-cli` and Eclipse
      Temurin JRE builds fetched at build time from GitHub/Adoptium. Those
      pins were resolved for `amd64`. If the new machine is ARM (e.g.
      Raspberry Pi, Apple Silicon host), verify Adoptium's apt repo and
      the pinned signal-cli release both publish an arm64 build before
      assuming `docker compose up -d --build` will just work — it may need
      the `ARG` versions bumped to arm64-available releases.
- [ ] Outbound internet access during build: the Dockerfile pulls from
      `packages.adoptium.net` and `github.com/AsamK/signal-cli` — needs to
      reach both the first time you build on the new machine.

## After `docker compose up -d --build` on the new machine

- [ ] Check logs for a clean start: `docker compose logs rotation-app`
      should show `signal-cli daemon ready (account +19165550100)` and
      `Signal bot listening`, not a crash loop.
- [ ] Confirm exactly **one** `java` process:
      `docker top sacramental-line-rotation-rotation-app-1` — if you see
      more than one, or one that keeps restarting, something's off (see
      project memory on the daemon-mode rework for what this should look
      like).
- [ ] Verify the dashboard is reachable at
      `http://<new-tailscale-ip>:8420` (not the old
      `100.93.169.35` / `hal9000.tail...` address — that was this
      machine's identity, not the app's) and **not** reachable from outside
      Tailscale.
- [ ] Text `STATUS` to the bot number from one of the priests' phones to
      confirm the Signal send/receive loop survived the move end-to-end.
- [ ] Update any bookmarks/shortcuts pointing at the old Tailscale
      hostname.

## Still-open items, unrelated to the move itself

- [ ] Automatic scheduling is **off** (since 13 Aug 2026) and goes live
      when a priest texts `ENABLE`. If you move hosts while it is still
      off, it stays off — that is not something the move broke.
- [ ] `RC_MODE=api-v2` and the RingCentral credentials live in `.env`,
      which is git-ignored. If `.env` does not come across, the app
      cannot write the ring at all. See "Copy the whole project folder"
      above — this is the single most important file to verify landed.
