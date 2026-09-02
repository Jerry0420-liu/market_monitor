#!/bin/sh
set -eu

DEPLOY_ROOT=${1:-/opt/market-monitor/deploy}
DATA_ROOT=${2:-/var/lib/market-monitor}
BACKUP_ROOT=${3:-/var/backups/market-monitor}

install -d -o 10001 -g 10001 -m 0700 "$DATA_ROOT"
install -d -o 10001 -g 10001 -m 0700 "$DATA_ROOT/artifacts"
install -d -o 10001 -g 10001 -m 0700 "$BACKUP_ROOT"
install -d -o root -g root -m 0700 "$DEPLOY_ROOT/secrets"

OWNER_FILE="$DEPLOY_ROOT/secrets/owner-password"
if [ ! -f "$OWNER_FILE" ]; then
    printf '%s\n' "missing owner-controlled secret file: $OWNER_FILE" >&2
    exit 2
fi
chown root:root "$OWNER_FILE"
chmod 0400 "$OWNER_FILE"

printf '%s\n' "host paths prepared; OFFICIAL remains disabled"
