#!/usr/bin/env bash
# Installs an AI coding assistant on this server so someone who is not a
# programmer can get help fixing the bot. Run:  sudo sacline  -> 9
# (or: sudo ops/install-ai-assistant.sh). Safe to re-run.
#
# Free tiers and prices change; check the current terms at the link shown
# before relying on one.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo $0"; exit 1; }
PROJECT=/home/padre/sacramental-line-rotation

cat <<'TXT'

 Which AI assistant?
  1  Gemini CLI (Google) - FREE with a personal Google account; good at this.
     https://github.com/google-gemini/gemini-cli
  2  Claude Code (Anthropic) - the one this system was built with; best results,
     needs a paid Claude account.  https://claude.com/claude-code
  3  Codex CLI (OpenAI) - needs a ChatGPT account.  https://github.com/openai/codex
  q  Cancel
TXT
read -r -p " Choose: " c
case "$c" in
  1) pkg=@google/gemini-cli;       cmd=gemini ;;
  2) pkg=@anthropic-ai/claude-code; cmd=claude ;;
  3) pkg=@openai/codex;            cmd=codex ;;
  *) exit 0 ;;
esac

if ! command -v npm >/dev/null || [ "$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)" -lt 20 ]; then
    echo "Installing Node.js (needed by all three)..."
    apt-get update -qq && apt-get install -y -qq nodejs npm >/dev/null
fi
echo "Installing $pkg ..."
npm install -g "$pkg" >/dev/null
command -v "$cmd" >/dev/null || { echo "Install failed; try again or see the link above."; exit 1; }

cat <<TXT

 Installed. Now, as your normal user (NOT with sudo):

   cd $PROJECT
   $cmd

 The first time, it asks you to sign in (a link to open in a browser).
 Then paste this as your first message:

   Read CLAUDE.md and HANDOFF.md first. I am not a programmer. The Signal bot for
   our emergency phone line has a problem: <describe it>. If I made a report
   (sudo sacline -> 2), it is in data/reports/. Explain what is wrong in plain
   language, propose the fix, and ask me before changing anything or running
   anything that restarts the bot.

 Remove it again when done (hand-off rule: no personal accounts left on the server):
   $cmd logout  (if it has one), then:  sudo npm uninstall -g $pkg
TXT
