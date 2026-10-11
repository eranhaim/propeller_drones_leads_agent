#!/usr/bin/env bash
#
# Install the host-level outage alerter on the production box.
#
# Run as root ON the server, from the repo checkout:
#   bash deploy/install-watch.sh
#
# Idempotent: re-running it upgrades the script and the units in place.
#
# It does NOT write /etc/propeller-watch.env. That file holds the GreenAPI
# credentials and is created once, by hand, from
# deploy/propeller-watch.env.example. Keeping it out of this script is what
# makes the alerter's configuration independent of the bot's .env.

set -euo pipefail

die() { printf '\ninstall failed: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "run as root"

SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG=/etc/propeller-watch.env

[ -f "$CONFIG" ] || die "$CONFIG is missing - copy deploy/propeller-watch.env.example there, fill it in, then chmod 600"

install -m 755 "$SOURCE_DIR/propeller_watch.py" /usr/local/bin/propeller-watch
install -m 644 "$SOURCE_DIR/propeller-watch.service" /etc/systemd/system/propeller-watch.service
install -m 644 "$SOURCE_DIR/propeller-watch.timer" /etc/systemd/system/propeller-watch.timer
install -d -m 700 /var/lib/propeller-watch

chown root:root "$CONFIG"
chmod 600 "$CONFIG"

systemctl daemon-reload
systemctl enable --now propeller-watch.timer

# Prove the check runs and reports before trusting the timer with it.
propeller-watch --dry-run

systemctl list-timers propeller-watch.timer --no-pager
