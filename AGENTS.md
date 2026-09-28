# Instructions for AI coding assistants

This project's full instructions are in **[CLAUDE.md](CLAUDE.md)** (written for Claude
Code, but they apply to any assistant). Read it first, then **[HANDOFF.md](HANDOFF.md)**.

The essentials:
- This bot controls a real parish emergency phone line. The line keeps ringing
  whatever happens here; a wrong change can make it ring the wrong priest.
- The person asking is probably not a programmer. Explain in plain language,
  propose the fix, and **ask before** changing files, restarting the bot
  (`docker compose ...`), or writing to RingCentral.
- Follow the "Rules that must stay true" in CLAUDE.md exactly.
- Troubleshooting tools already exist: `sudo sacline` (server menu), the Signal
  command `TROUBLESHOOT`, reports in `data/reports/`, the event log
  `data/ops-journal.jsonl`. See docs/TROUBLESHOOTING.md.
- Run the tests before and after a code change (see CLAUDE.md, "Run").
