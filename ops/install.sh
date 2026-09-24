#!/usr/bin/env bash
# One-time setup (safe to re-run) for backups, automatic updates, and the
# watchdog. Run from anywhere:
#
#   sudo ~/sacramental-line-rotation/ops/install.sh
#
# Re-running it later just reinstalls the scripts/units from ops/ (after
# you edit them) - it never re-formats a backup drive that's already set up.
#
#   1. Encrypted backup USB: wipes the drive, LUKS2 encryption with a
#      passphrase YOU choose (write it down - it's needed to restore on a
#      different machine) plus a keyfile kept on this machine so nightly
#      backups run unattended.
#   2. unattended-upgrades: Debian + security + Docker + Tailscale updates,
#      automatic reboot at midnight only when an update needs one.
#   3. systemd timers: backup 2:30 AM nightly, watchdog every 5 minutes,
#      signal-cli version check Tuesdays 10 AM (the bot asks the priests
#      Y/N only when a newer version exists; Y installs it automatically),
#      and a boot-time check that starts the bot if it was left stopped.

set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo: sudo $0"; exit 1; }

OPS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(dirname "$OPS")"
LIB=/usr/local/lib/sacline
ETC=/etc/sacline
CONF=$ETC/backup.conf
KEYFILE=$ETC/usb-backup.key
IMAGE=sacramental-line-rotation:latest
# Backup drive: Kingston SV300 240 GB SSD in a USB enclosure, swapped in on
# 23 Sep 2026 after the first SanDisk stick turned out to be worn out
# (hardware read-only). Pass a different /dev/disk/by-id/usb-... path as $1
# to set up a replacement drive.
USB_DISK="${1:-/dev/disk/by-id/usb-KINGSTON_SV300S37A240G_20D11E80C229-0:0}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "Installing packages (cryptsetup, unattended-upgrades)"
DEBIAN_FRONTEND=noninteractive apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cryptsetup unattended-upgrades apt-listchanges >/dev/null
mkdir -p "$ETC" "$LIB" /var/lib/sacline
chmod 700 "$ETC"

# ---------------------------------------------------------------- 1. USB
if [ -f "$CONF" ]; then
    say "Backup drive already set up ($CONF) - not touching it"
else
    say "Setting up the encrypted backup USB drive"
    # Subshell so a drive problem stops only this step, not the whole install.
    set +e
    (
    set -e
    [ -b "$USB_DISK" ] || { echo "Drive not found: $USB_DISK  (plugged in?)"; exit 3; }
    DISK=$(readlink -f "$USB_DISK")
    [ "$(lsblk -dno TRAN "$DISK")" = "usb" ] || { echo "$DISK is not a USB drive - refusing."; exit 3; }
    ROOTDISK=$(lsblk -no PKNAME "$(findmnt -no SOURCE /)")
    [ "$(basename "$DISK")" != "$ROOTDISK" ] || { echo "$DISK holds the running system - refusing."; exit 3; }
    if [ "$(cat "/sys/block/$(basename "$DISK")/ro")" = "1" ]; then
        blockdev --setrw "$DISK" 2>/dev/null || true
        if [ "$(cat "/sys/block/$(basename "$DISK")/ro")" = "1" ]; then
            echo "$DISK is READ-ONLY at the hardware level (kernel: $(dmesg | grep -i "$(basename "$DISK")" | grep -i 'write protect' | tail -1 | sed 's/^\[[^]]*\] //'))."
            echo "A SanDisk stick does this permanently when its flash wears out. Nothing was changed."
            echo "Use a different drive:  sudo $0 /dev/disk/by-id/usb-<new drive>"
            exit 3
        fi
    fi

    echo
    lsblk -o NAME,SIZE,FSTYPE,LABEL,MODEL "$DISK"
    echo
    echo "Current contents of the drive (read-only look):"
    TMPM=$(mktemp -d)
    for part in $(lsblk -lnpo NAME "$DISK" | tail -n +2); do
        if mount -o ro "$part" "$TMPM" 2>/dev/null; then
            echo "  [$part]"; ls -la "$TMPM" | head -20 | sed 's/^/    /'
            du -sh "$TMPM" 2>/dev/null | sed 's/^/    total used: /'
            umount "$TMPM"
        fi
    done
    rmdir "$TMPM"
    echo
    echo "EVERYTHING ON $DISK WILL BE PERMANENTLY ERASED."
    read -r -p "Type ERASE to continue: " answer
    [ "$answer" = "ERASE" ] || { echo "Cancelled. Nothing was changed."; exit 3; }

    for part in $(lsblk -lnpo NAME "$DISK" | tail -n +2); do umount "$part" 2>/dev/null || true; done
    wipefs -a "$DISK" >/dev/null
    echo 'label: gpt
,,L' | sfdisk -q "$DISK"
    udevadm settle
    PART=$(lsblk -lnpo NAME "$DISK" | sed -n 2p)

    echo
    echo "Choose the drive's PASSPHRASE now. Write it down and keep it somewhere"
    echo "safe away from this computer (e.g. the parish safe). Without it the"
    echo "backups cannot be opened on a replacement machine."
    echo "(cryptsetup will ask you to type YES in capitals, then the passphrase twice.)"
    cryptsetup luksFormat --type luks2 --label SACLINE-BACKUP "$PART"

    head -c 64 /dev/urandom > "$KEYFILE"
    chmod 400 "$KEYFILE"
    echo "Enter the same passphrase once more to authorize this machine's automatic key:"
    cryptsetup luksAddKey "$PART" "$KEYFILE"

    cryptsetup open --key-file "$KEYFILE" "$PART" sacline-setup
    mkfs.ext4 -q -L sacline-backup /dev/mapper/sacline-setup
    cryptsetup close sacline-setup

    LUKS_UUID=$(cryptsetup luksUUID "$PART")
    cat > "$CONF" <<EOF
LUKS_UUID=$LUKS_UUID
KEYFILE=$KEYFILE
PROJECT=$PROJECT
IMAGE=$IMAGE
EOF
    chmod 600 "$CONF"
    echo "Drive encrypted and formatted (LUKS UUID $LUKS_UUID)."
    )
    usb_rc=$?
    set -e
    if [ $usb_rc -ne 0 ]; then
        echo
        read -r -p "Backup drive NOT set up. Continue installing automatic updates + watchdog without backups? [Y/n] " c
        [ "${c,,}" != "n" ] || exit 1
    fi
fi

# --------------------------------------------------------- 2. auto-updates
say "Configuring automatic updates (reboot at midnight when required)"
install -m 644 "$OPS/apt/20auto-upgrades" /etc/apt/apt.conf.d/20auto-upgrades
install -m 644 "$OPS/apt/52sacline-unattended-upgrades" /etc/apt/apt.conf.d/52sacline-unattended-upgrades
systemctl enable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null
unattended-upgrade --dry-run >/dev/null 2>&1 && echo "unattended-upgrades dry run OK." \
    || echo "WARNING: unattended-upgrades dry run reported a problem: run 'unattended-upgrade --dry-run -d'"

# ------------------------------------------------------ 3. scripts + timers
say "Installing backup + watchdog scripts and timers"
install -m 755 "$OPS/backup.sh" "$LIB/backup.sh"
install -m 755 "$OPS/watchdog.py" "$LIB/watchdog.py"
install -m 644 "$OPS/send_alert.py" "$LIB/send_alert.py"
install -m 755 "$OPS/signal_update_check.py" "$LIB/signal_update_check.py"
install -m 755 "$OPS/signal_update.sh" "$LIB/signal_update.sh"
install -m 755 "$OPS/boot_start.sh" "$LIB/boot_start.sh"
install -m 644 "$OPS"/systemd/sacline-*.service "$OPS"/systemd/sacline-*.timer "$OPS"/systemd/sacline-*.path /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sacline-watchdog.timer >/dev/null
# At every boot: start the bot if a power cut left it stopped (runs next boot).
systemctl enable sacline-boot-start.service >/dev/null
# Weekly signal-cli check (Tue 10 AM; silent unless there's a new version)
# and the Y-answer trigger for the automatic update.
systemctl enable --now sacline-signal-check.timer sacline-signal-update.path sacline-signal-update.timer >/dev/null

if [ -f "$CONF" ]; then
    systemctl enable --now sacline-backup.timer >/dev/null
    say "Running a backup now"
    if systemctl start sacline-backup.service; then
        python3 -c 'import json;print(json.load(open("/var/lib/sacline/backup-status.json"))["last_message"])'
    else
        echo "BACKUP FAILED:"; journalctl -u sacline-backup -n 20 --no-pager
    fi
else
    systemctl disable --now sacline-backup.timer >/dev/null 2>&1 || true
    say "Backups skipped: no drive set up yet. Re-run this script with the new drive plugged in."
fi

say "signal-cli version check (no texts sent)"
"$LIB/signal_update_check.py" --dry-run || true

say "Watchdog check (no texts sent)"
"$LIB/watchdog.py" --status || true

echo
read -r -p "Send a one-time TEST text to all the priests now (Signal + SMS)? [y/N] " t
if [ "${t,,}" = "y" ]; then "$LIB/watchdog.py" --test-alert || true; fi

say "Done"
systemctl list-timers --no-pager 'sacline-*' apt-daily-upgrade.timer
echo
echo "NOTE: if app/ changed (e.g. the Signal update Y/N handling), rebuild the bot:"
echo "    cd $PROJECT && docker compose up -d --build"
