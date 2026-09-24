#!/usr/bin/env bash
# One-time setup for a fresh Debian box that will host the Sacramental
# Emergency Line rotation app. See ../docs/MIGRATION.md for the full
# move checklist — this script only covers the "install Docker, Tailscale,
# and unattended security upgrades" part of it.
#
# Written for Debian 12 (bookworm) or 13 (trixie), headless, with the
# "SSH server" + "standard system utilities" install profile. Should be
# run as a sudo-capable non-root user, e.g.:
#
#   scp docs/new-machine-bootstrap.sh youruser@<new-machine-ip>:~/
#   ssh youruser@<new-machine-ip> 'chmod +x new-machine-bootstrap.sh && ./new-machine-bootstrap.sh'
#
# It is a plain shell script — read it before running it, same as any
# script that asks for sudo. It does NOT run `tailscale up` (that needs
# an interactive browser login) and does NOT touch this project's files
# at all; copy those over separately per MIGRATION.md.

set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal sudo user, not as root." >&2
    exit 1
fi

. /etc/os-release
echo "Detected: ${PRETTY_NAME:-unknown OS} (codename: ${VERSION_CODENAME:-unknown})"
if [ "${ID:-}" != "debian" ]; then
    echo "This script assumes Debian. It may still work elsewhere, but hasn't been checked there." >&2
fi

echo "==> Updating the base system"
sudo apt-get update
sudo apt-get -y upgrade

echo "==> Installing prerequisites"
sudo apt-get install -y ca-certificates curl gnupg

echo "==> Adding Docker's official apt repository"
# Distro-packaged docker.io lags upstream and sometimes lacks the
# Compose v2 plugin outright; Docker's own repo is what docker-compose.yml
# here is written against (Compose V2's ${VAR:?...} required-var syntax).
sudo install -m 0755 -d /etc/apt/keyrings
if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
    curl -fsSL "https://download.docker.com/linux/debian/gpg" \
        | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
fi
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian \
  ${VERSION_CODENAME} stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

echo "==> Installing Docker Engine + Compose v2 plugin"
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

echo "==> Adding $(whoami) to the docker group (log out/in for this to take effect)"
sudo usermod -aG docker "$(whoami)"

echo "==> Enabling Docker to start on boot"
sudo systemctl enable --now docker

echo "==> Installing Tailscale"
# Official install method from https://tailscale.com/download/linux —
# adds Tailscale's apt repo and installs the package, but does not
# authenticate the machine.
curl -fsSL https://tailscale.com/install.sh | sh

echo "==> Enabling unattended security upgrades"
# This box is meant to sit untouched for years (see CLAUDE.md's "keep
# running unattended" ethos for the app itself) — the OS should get the
# same treatment for security patches, without needing anyone to log in.
sudo apt-get install -y unattended-upgrades apt-listchanges
sudo dpkg-reconfigure -f noninteractive unattended-upgrades

echo
echo "==> Done. Still to do:"
echo "    1. Log out and back in (or 'newgrp docker') to pick up the docker group."
echo "    2. Run: sudo tailscale up"
echo "       (follow the printed URL to authenticate this machine to your tailnet)"
echo "    3. Run: tailscale ip -4"
echo "       (you'll need this for TAILSCALE_IP in .env — see docs/MIGRATION.md)"
echo "    4. Copy the project over (docs/MIGRATION.md, 'Before you leave the old machine')"
echo "    5. cd into the project folder and run: docker compose up -d --build"
