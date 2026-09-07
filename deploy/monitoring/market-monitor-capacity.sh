#!/bin/sh
set -eu

DATA_DIR=${MARKET_MONITOR_HOST_DATA_DIR:-/var/lib/market-monitor}
BACKUP_DIR=${MARKET_MONITOR_HOST_BACKUP_DIR:-/var/backups/market-monitor}
MIN_FREE_PERCENT=${MARKET_MONITOR_MIN_FREE_PERCENT:-20}
MAX_BACKUP_SETS=${MARKET_MONITOR_MAX_BACKUP_SETS:-3}

check_path() {
    label=$1
    path=$2
    if [ ! -d "$path" ]; then
        printf '%s path_missing=%s\n' "$label" "$path"
        return 2
    fi
    used_percent=$(df -P "$path" | awk 'NR==2 {gsub(/%/, "", $5); print $5}')
    free_percent=$((100 - used_percent))
    size_bytes=$(du -sb "$path" | awk '{print $1}')
    printf '%s size_bytes=%s free_percent=%s\n' "$label" "$size_bytes" "$free_percent"
    [ "$free_percent" -ge "$MIN_FREE_PERCENT" ]
}

status=0
check_path data "$DATA_DIR" || status=$?
check_path backup "$BACKUP_DIR" || status=$?
if [ -d "$BACKUP_DIR" ]; then
    backup_sets=$(find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type d ! -name '.*' | wc -l | tr -d ' ')
    printf 'backup_sets=%s max_backup_sets=%s\n' "$backup_sets" "$MAX_BACKUP_SETS"
    [ "$backup_sets" -le "$MAX_BACKUP_SETS" ] || status=2
fi
exit "$status"
