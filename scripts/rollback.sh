#!/bin/sh
# Roll back. Never deletes /data/battery-aggregator (logs, recordings, backups stay).
#   sh rollback.sh --to-shadow [--dry-run]        back to shadow mode + restore Venus settings
#   sh rollback.sh --to-version VER [--dry-run]   switch 'current' to another release + restart
#   sh rollback.sh --disable [--dry-run]          stop service, remove /service link + boot hook,
#                                                 restore Venus settings
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/lib.sh"
MODE=""
VER=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --to-shadow) MODE=shadow ;;
        --disable) MODE=disable ;;
        --to-version) MODE=version; shift; VER="${1:-}" ;;
        *) die "unknown argument $1" ;;
    esac
    shift
done
[ -n "$MODE" ] || die "choose --to-shadow | --to-version VER | --disable"
need_root

restore_settings() {
    LAST="$(ls -1 "$BASE"/backup/settings-*.txt 2>/dev/null | head -n 1)"   # oldest = pre-install
    if [ -z "$LAST" ]; then
        say "no settings backup found - set controlling BMS / battery monitor manually"
        return 0
    fi
    BMS="$(sed -n 's/^BmsInstance=//p' "$LAST" | head -n 1)"
    MON="$(sed -n 's/^BatteryService=//p' "$LAST" | head -n 1)"
    say "restoring settings from $LAST: BmsInstance=$BMS BatteryService=$MON"
    # the backup holds raw dbus-cli output (strings in quotes): clean + validate + read back
    [ -n "$BMS" ] && restore_setting BmsInstance "$SET_BMS" "$BMS"
    [ -n "$MON" ] && restore_setting BatteryService "$SET_MON" "$MON"
    return 0
}

case "$MODE" in
    shadow)
        set_shadow true
        restore_settings
        svc_ctl -t
        ;;
    version)
        [ -n "$VER" ] && [ -d "$BASE/releases/$VER" ] || die "release '$VER' not found in $BASE/releases"
        run ln -sfn "$BASE/releases/$VER" "$BASE/current"
        svc_ctl -t
        ;;
    disable)
        restore_settings
        svc_ctl -d
        run touch "$BASE/disabled"
        if [ -L "$SERVICE_LINK" ]; then run rm "$SERVICE_LINK"; fi
        if [ -f "$RC_LOCAL" ] && grep -q "$MARK" "$RC_LOCAL"; then
            say "removing boot hook from $RC_LOCAL"
            if [ "$DRY_RUN" = "1" ]; then
                echo "DRY-RUN: remove hook lines from $RC_LOCAL"
            else
                cp "$RC_LOCAL" "$BASE/backup/rc.local.bak"
                awk -v m="$MARK" '$0==m{skip=1;next} skip==1{skip=0;next} {print}' "$BASE/backup/rc.local.bak" > "$RC_LOCAL"
            fi
        fi
        ;;
esac
say "rollback ($MODE) done"
