#!/bin/sh
# OWNER STEP: leave shadow mode and make the aggregate the controlling BMS + battery monitor.
#   sh activate.sh [--dry-run]
# Refuses if the config is invalid for live mode (missing commissioning inputs, DECISIONS.md D4).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/lib.sh"
for a in "$@"; do
    case "$a" in
        --dry-run) DRY_RUN=1 ;;
        *) die "unknown argument $a" ;;
    esac
done
need_root
[ -f "$BASE/config.json" ] || die "no config.json - run install.sh first"

TS="$(date +%Y%m%d-%H%M%S)"
run cp "$BASE/config.json" "$BASE/backup/config-$TS.json"
set_shadow false
if [ "$DRY_RUN" != "1" ]; then
    if ! (cd "$BASE/current" && "$PY" -m battery_aggregator.main --check-config --config "$BASE/config.json"); then
        cp "$BASE/backup/config-$TS.json" "$BASE/config.json"
        die "config not valid for live mode - restored shadow config"
    fi
fi
INST="$("$PY" -c "import json; print(json.load(open('$BASE/config.json')).get('device_instance', 512))" 2>/dev/null || echo 512)"
{
    echo "BmsInstance=$(dbus_get $SETTINGS_SVC $SET_BMS)"
    echo "BatteryService=$(dbus_get $SETTINGS_SVC $SET_MON)"
} > "$BASE/backup/settings-$TS.txt" 2>/dev/null || true
say "restarting service (live mode)"
svc_ctl -t
say "setting controlling BMS / battery monitor to instance $INST"
dbus_set $SETTINGS_SVC $SET_BMS "$INST"
dbus_set $SETTINGS_SVC $SET_MON "com.victronenergy.battery/$INST"
say "done. Live acceptance: S01, S02, S22 (USB at moderate load), S31 - see docs/03_SZENARIEN.md"
say "rollback: sh $BASE/current/scripts/rollback.sh --to-shadow"
