# Hand-off checklist

Use this when the server is handed over to the parish (or a new admin) and the original
admin's personal accounts come off it. Tick each box. Do it in order: the new owner's access
has to work **before** the old access is removed.

## 1. Before: the new owner's access
- [ ] Parish-owned **Tailscale** account created, and the server joined to it
      (`sudo tailscale logout` then `sudo tailscale up`, signed in as the parish).
      The dashboard is only reachable over Tailscale. If the server's Tailscale address
      changes, update `TAILSCALE_IP` in `.env`, then `docker compose up -d`.
- [ ] New admin can SSH in over the parish tailnet and run `sudo sacline` → 1 Status.
- [ ] New admin has a Linux login with sudo (or the `padre` password has been changed and handed over).
- [ ] Parish-owned **GitHub** account (or organization) exists.

## 2. Move ownership
- [ ] Transfer the GitHub repo `horacanonica/sacramentalemergency-server` to the parish account
      (GitHub → Settings → Transfer). Then on the server:
      `git remote set-url origin git@github.com:<parish>/sacramentalemergency-server.git`
      and update the link in `app/doctor.py` (`REPO_URL`).
- [ ] **RingCentral:** decide which RingCentral admin user owns the API app. Create a new JWT
      under that user and put it in `.env` as `RC_JWT` (**still pending** since 23 Sep 2026).
      If the app itself moves, also update `RC_CLIENT_ID` / `RC_CLIENT_SECRET`.
      Restart with `sudo sacline` → 3, then check with → 1.
- [ ] **Email alerts:** change `SMTP_PASSWORD` (and `SMTP_USERNAME` / `ALERT_EMAIL_TO` if the
      mailbox changes).
- [ ] **Dashboard:** new `WEB_ADMIN_PASSWORD` and a new random `FLASK_SECRET_KEY` in `.env`.
- [ ] **Signal bot number:** write down who owns the phone number/SIM the bot is registered with,
      and keep it active. Losing it means re-registering Signal.

## 3. Remove the old admin's personal accounts
- [ ] **Claude Code:** `claude` → `/logout`, then uninstall it
      (`sudo npm uninstall -g @anthropic-ai/claude-code`, or delete `~/.local/bin/claude` if it was
      installed natively), then `rm -rf ~/.claude` (settings and memory).
- [ ] **GitHub SSH key:** delete `~/.ssh/id_ed25519_github` and `.pub`, and remove that key on
      GitHub (Settings → SSH keys). Check `~/.ssh/config` for entries that point to it.
- [ ] Any other AI assistant installed through `sacline` → 9: sign out and uninstall.
- [ ] Browser logins, saved passwords, or personal files in `~/Downloads`, `~/Documents`:
      move or delete them.
- [ ] Old admin's Tailscale device and account access removed from the parish tailnet.

## 4. The sealed envelope / parish password manager
- [ ] Server Tailscale name, Linux login and password
- [ ] Parish Tailscale account login
- [ ] USB backup drive **passphrase** (set up 23 Sep 2026; without it, backups can't be opened on
      another machine)
- [ ] Dashboard login (`WEB_ADMIN_USERNAME` / `WEB_ADMIN_PASSWORD`)
- [ ] RingCentral admin login
- [ ] Parish GitHub login
- [ ] Who owns the Signal bot's phone number
- [ ] This checklist, docs/TROUBLESHOOTING.md, ops/RESTORE.md (printed copies)

## 5. Check that everything still works
- [ ] `sudo sacline` → 1 Status: all OK
- [ ] A priest texts **TROUBLESHOOT** → **5** and receives the report file
- [ ] `sudo /usr/local/lib/sacline/watchdog.py --test-alert`: every priest gets the test text
- [ ] The next morning, the nightly backup is logged as OK in `sacline` → 1
- [ ] The next evening, `data/ops-journal.jsonl` has an "8 PM check" entry
- [ ] Nothing personal is left: `ls ~/.claude ~/.ssh` shows no old keys or settings
