# Restoring the Sacramental Emergency Line bot from the backup USB

The backup drive (Kingston 240 GB SSD in a USB enclosure, LUKS label `SACLINE-BACKUP`) is **encrypted**. To open
it on any machine other than the original server you need the **passphrase**
that was written down when it was set up. The original server unlocks it
on its own with a key file.

Drive layout:

```
RESTORE.md                      this file
snapshots/sacline-YYYY-MM-DD_HHMM.tar.gz   the whole project folder, one per night
                                           (nightly for 60 days, then 1 per month)
images/sacline-image-*.tar.gz   the built Docker image (last 3 versions)
```

Each archive has a `.sha256` file next to it for checking.

## 1. Get a Linux machine ready

Any x86-64 Linux box with Docker and Tailscale works (see `docs/MIGRATION.md`
in the project for hardware notes). Install Docker and Tailscale, then log into
Tailscale. For Docker on Debian, extract the snapshot first (step 3 works
without Docker) and run `sudo ops/install-docker.sh` from it. It adds the
user `padre` to the docker group, so edit that line if the new login differs.

**Stop the old server first if it's still running at all**
(`docker compose down` in the project folder). Two copies of the bot
running at once on the same Signal account corrupt its keys.

## 2. Unlock and mount the drive

```bash
sudo apt install cryptsetup
lsblk                                   # find the USB, e.g. /dev/sdb1
sudo cryptsetup open /dev/sdb1 sacline-backup     # asks for the passphrase
sudo mkdir -p /mnt/sacline-backup
sudo mount /dev/mapper/sacline-backup /mnt/sacline-backup
ls /mnt/sacline-backup/snapshots | tail  # newest is last
```

On a desktop you can also just plug it in: the file manager asks for the
passphrase.

## 3. Restore the project folder

```bash
cd /mnt/sacline-backup/snapshots
sha256sum -c sacline-2026-09-24_0230.tar.gz.sha256      # use the newest file name
mkdir -p ~/sacramental-line-rotation
tar xzf sacline-2026-09-24_0230.tar.gz -C ~/sacramental-line-rotation
```

That brings back `.env` (credentials), `config/priests.yaml`, `data/`
(rotation state + history), `signal-cli-data/` (the bot's Signal
registration: **do not re-register the number**), the code, and `ops/`.

## 4. Load the Docker image (skip the build)

```bash
cd /mnt/sacline-backup/images
docker load < "$(ls -t sacline-image-*.tar.gz | head -1)"
```

If this fails, `docker compose build` rebuilds it from the Dockerfile,
provided the pinned Java/signal-cli downloads still exist.

## 5. Fix the one machine-specific setting, then start it

```bash
cd ~/sacramental-line-rotation
tailscale ip -4                 # put this value in .env as TAILSCALE_IP=
nano .env
docker compose up -d            # no --build needed if step 4 worked
docker compose logs -f rotation-app
```

Text `STATUS` to the bot from a priest's phone to confirm it works.

## 6. Put the safety nets back

```bash
sudo ~/sacramental-line-rotation/ops/install.sh
```

This sets up automatic updates, the watchdog, and nightly backups again.
If the project isn't at `/home/padre/sacramental-line-rotation` on the
new machine, first update `PROJECT` at the top of `ops/watchdog.py`.
To keep using **this same drive**, first give the new machine its own
key so `install.sh` doesn't try to re-format it:

```bash
sudo mkdir -p /etc/sacline && sudo chmod 700 /etc/sacline
sudo sh -c 'head -c 64 /dev/urandom > /etc/sacline/usb-backup.key && chmod 400 /etc/sacline/usb-backup.key'
sudo cryptsetup luksAddKey /dev/sdb1 /etc/sacline/usb-backup.key     # asks for the passphrase
sudo sh -c "cat > /etc/sacline/backup.conf" <<EOF
LUKS_UUID=$(sudo cryptsetup luksUUID /dev/sdb1)
KEYFILE=/etc/sacline/usb-backup.key
PROJECT=$HOME/sacramental-line-rotation
IMAGE=sacramental-line-rotation:latest
EOF
```

When you're done: `sudo umount /mnt/sacline-backup && sudo cryptsetup close sacline-backup`.

## Restoring just one file (the server is fine, a file got damaged)

`data/state.json` usually repairs itself: every save keeps the previous
version as `data/state.json.bak`, and if `state.json` is unreadable at
startup (e.g. after a power cut) the bot uses the `.bak`, keeps the damaged
file as `state.json.damaged-<date>`, and texts the priests to check the
order. The steps below are for when both copies are bad (the bot won't
start and its log says so), or for any other file.

```bash
sudo systemctl stop sacline-backup.timer      # keep the nightly job from interrupting
sudo cryptsetup open --key-file /etc/sacline/usb-backup.key /dev/disk/by-label/SACLINE-BACKUP sacline-backup
sudo mount /dev/mapper/sacline-backup /mnt/sacline-backup
cd ~/sacramental-line-rotation && docker compose stop
tar xzf /mnt/sacline-backup/snapshots/<file>.tar.gz ./data/state.json     # e.g.
docker compose start
sudo umount /mnt/sacline-backup && sudo cryptsetup close sacline-backup
sudo systemctl start sacline-backup.timer
```
