# Troubleshooting the Sacramental Emergency Line bot

No programming knowledge needed. Work down this page and stop as soon as the problem is fixed.

**Whatever happens with the bot, the phone line itself keeps ringing.** RingCentral keeps the
last ring order it was given. The worst a broken bot can do is leave the wrong priest first in
line. Until it's fixed, you can always change the order by hand in the RingCentral app:
Incoming calls → Ring settings → Ring in order.

---

## Step 1 - Text TROUBLESHOOT to the bot

Any priest can do this. The bot asks what's wrong:

| What you see | Reply | What the bot does |
|---|---|---|
| Calls are going to the wrong priest | **1** | Compares RingCentral with the bot. If they differ, offers to send the bot's order to RingCentral, or to take RingCentral's order as correct (if someone changed it on purpose). |
| The bot is slow or ignores your commands | **2** | Shows anyone who is in the middle of a menu and offers to clear it, or restarts the bot (about a minute). |
| You got "Automatic switching is DISABLED" | **3** | Tells you why it happened. If RingCentral can be reached again, offers to turn automatic switching back on. |
| A priest can't use the bot, or you have a new priest | **4** | Looks him up in the bot, among recently deleted priests, and on the RingCentral ring, then offers to restore or add him. |
| You want someone else (or an AI) to look at it | **5** | Sends you a report file. See Step 2. |
| Nothing above helped | **6** | Sends the report, plus the server steps (Step 3). |

Text **CANCEL** to leave the wizard at any time.

## Step 2 - Ask an AI, using the report (no installing, from your phone)

1. Text **TROUBLESHOOT**, then **5**. The bot sends you a file named `report-....txt`.
2. Open any AI chat app or website. Free versions of these work: Claude (claude.ai),
   ChatGPT (chatgpt.com), Gemini (gemini.google.com).
3. Attach the file (or open it, copy everything, and paste it) and write:

   > This is a report from our parish's emergency phone-line bot. What is wrong, and how do I
   > fix it? I am not a programmer; give me exact steps, one at a time.

The report shows only the last 4 digits of phone numbers and contains no passwords. It includes
a link to the bot's code and documentation, so the AI can read how everything works.

If the AI's fix is something you can do by text (ENABLE, SETTINGS, ROTATE...) or in the
RingCentral app, do it. If it needs the server, go to Step 3.

## Step 3 - The server menu

### What you need
From the sealed hand-off envelope or the parish password manager:
- the server's **Tailscale name**
- the **server login name and password**
- an account on the parish **Tailscale** network, which is what lets your device reach the server

### Connect
1. Install **Tailscale** on your computer or phone (tailscale.com/download) and sign in with the
   parish Tailscale account.
2. Open a terminal:
   - **Mac:** open the *Terminal* app.
   - **Windows:** open *PowerShell* from the Start menu.
   - **iPhone/iPad or Android:** install the free app *Termius*.
3. Type the following, using the name and login from the envelope, then enter the password when
   asked (nothing shows while you type; that's normal):

   ```
   ssh LOGIN@SERVER-NAME
   ```

4. Type:

   ```
   sudo sacline
   ```

   Enter the password again if asked. A numbered menu appears:

| # | Option | When to use it |
|---|---|---|
| 1 | Status | Always first. Checks everything and changes nothing. |
| 2 | Make a report | Same report as TROUBLESHOOT 5, plus server details (Docker, disk, recent log). |
| 3 | Restart the bot | Bot not answering, or Signal is down. |
| 4 | Re-send the ring | RingCentral rings the wrong priest and the bot's order (STATUS) is right. |
| 5 | Restore saved data | Only after a "saved data was damaged" message. |
| 6 | Earlier program version | Something broke right after a program change. **6b** undoes it. |
| 7 | USB backup restore | Machine dead or disk wiped. Instructions only. |
| 8 | Review packet | For the regular operations review (see OPERATIONS-REVIEW.md). |
| 9 | Install an AI assistant | When nothing above fixes it. See Step 4. |

Every option explains what it will do and asks before changing anything.

## Step 4 - An AI assistant on the server

This lets an AI read the bot's code on the server and fix it with you.

1. `sudo sacline`, then **9**, then choose one:
   - **Gemini CLI**: free with a personal Google account. Recommended if you have no paid AI account.
   - **Claude Code**: what the bot was built with, and the best at this. Needs a paid Claude account.
   - **Codex CLI**: needs a ChatGPT account.
   Free plans change, so check the current terms at the link shown.
2. Quit the menu (**q**), then type the following, **without** `sudo`, where `gemini` is the name
   the installer printed (`claude` or `codex` for the others):

   ```
   cd ~/sacramental-line-rotation
   gemini
   ```

3. The first time, it shows a link. Open it in a browser and sign in.
4. Paste this as your first message, then describe the problem:

   > Read CLAUDE.md and HANDOFF.md first. I am not a programmer. The Signal bot for our
   > emergency phone line has a problem: … If I made a report (sudo sacline → 2), it is in
   > data/reports/. Explain what is wrong in plain language, propose the fix, and ask me before
   > changing anything or running anything that restarts the bot.

5. Read what it proposes before saying yes. **Say no** to anything that would delete
   `signal-cli-data`, `.env`, `config/` or `data/`, or that uses `docker compose down -v`. Those
   hold the bot's Signal registration, passwords and all saved settings.
6. When you're done, sign out and uninstall it (the installer prints how). Personal accounts
   should not stay on the server.

## Calls to the line

- Text **CALLS** for the last 24 hours, with full numbers. **CALLS 30** gives the last 30 days
  (last 4 digits only). **CALLS REPORT** gives statistics plus a spreadsheet.
- On the server: `sudo sacline` → **c**.
- In the RingCentral app, signed in as the Sacramental Emergency Line extension: the call history
  shows full numbers for as long as RingCentral keeps them.
- Missed calls with no voicemail are texted to the priests on the line automatically. If those
  texts stop, check `sacline` → 1 (the bot must be running and able to reach RingCentral).

## Where things are (for the AI or a helper)

| What | Where |
|---|---|
| Rules and design | `CLAUDE.md`, `HANDOFF.md`, `AGENTS.md` |
| Event log (every switch, failure, fix) | `data/ops-journal.jsonl` |
| Bot log | `data/app.log`, or `docker compose logs rotation-app` |
| Reports | `data/reports/` (kept 30 days) |
| Call log | `data/call-log.jsonl` (last 4 digits), `data/calls-recent.jsonl` (full numbers, 24 hours) |
| Saved state | `data/state.json` (previous save: `state.json.bak`) |
| Priest list / allowlist | `config/priests.yaml` |
| Server checks | `sudo /usr/local/lib/sacline/watchdog.py --status` |
| Full restore from USB | `ops/RESTORE.md` |
