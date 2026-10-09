# Shared helpers for install/activate/rollback (POSIX sh, BusyBox compatible).
# ROOT prefixes every device path (tests run with ROOT=<tmpdir>); DRY_RUN=1 only prints.

ROOT="${ROOT:-}"
DRY_RUN="${DRY_RUN:-0}"
BASE="$ROOT/data/battery-aggregator"
SERVICE_LINK="$ROOT/service/battery-aggregator"     # VERIFY: /service mechanics on Venus v3.70
RC_LOCAL="$ROOT/data/rc.local"
LOG_DIR="$ROOT/data/log/battery-aggregator"
PY="${PY:-python3}"
SETTINGS_SVC="com.victronenergy.settings"
SET_BMS="/Settings/SystemSetup/BmsInstance"          # VERIFY path + value type in v3.70
SET_MON="/Settings/SystemSetup/BatteryService"       # VERIFY path + value format in v3.70
MARK="# battery-aggregator boot hook"

say() { echo "[battery-aggregator] $*"; }
die() { echo "[battery-aggregator] ERROR: $*" >&2; exit 1; }

# run CMD...: execute, or only print in dry-run mode
run() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "DRY-RUN: $*"
    else
        "$@" || die "command failed: $*"
    fi
}

need_root() {
    [ "$DRY_RUN" = "1" ] && return 0
    [ -n "$ROOT" ] && return 0
    [ "$(id -u)" = "0" ] || die "must run as root on the Cerbo"
}

# dbus_get SERVICE PATH -> prints value ('' if unavailable). Uses Venus 'dbus' CLI (VERIFY).
dbus_get() {
    if command -v dbus >/dev/null 2>&1; then
        dbus -y "$1" "$2" GetValue 2>/dev/null
    fi
}

dbus_set() {
    run dbus -y "$1" "$2" SetValue "$3"
}

# clean_setting NAME RAW -> prints RAW without the quotes the Venus 'dbus' CLI prints around
# strings ('com.victronenergy.battery/3' -> com.victronenergy.battery/3). Returns 1 (prints
# nothing) when the result is not a plausible value for NAME - such a value is never written.
clean_setting() {
    v="$(printf '%s' "$2" | tr -d '\r\n' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    case "$v" in
        \'*\'|\"*\") v="${v#?}"; v="${v%?}" ;;      # strip one pair of quotes
    esac
    case "$1" in
        BmsInstance) printf '%s' "$v" | grep -Eq '^-?[0-9]+$' || return 1 ;;
        BatteryService) printf '%s' "$v" | grep -Eq '^[A-Za-z0-9._/-]+$' || return 1 ;;
        *) return 1 ;;
    esac
    printf '%s\n' "$v"
}

# restore_setting NAME DBUS_PATH RAW: write the cleaned backup value, then read it back and
# compare. An unclean backup value is skipped (manual step), never written.
restore_setting() {
    val="$(clean_setting "$1" "$3")" || { say "backup value for $1 not usable ($3) - not restored, set it manually"; return 0; }
    dbus_set "$SETTINGS_SVC" "$2" "$val"     # VERIFY: dbus-cli treats non-numeric text as string
    [ "$DRY_RUN" = "1" ] && return 0
    back="$(clean_setting "$1" "$(dbus_get "$SETTINGS_SVC" "$2")")" || back=""
    if [ "$back" = "$val" ]; then
        say "$1 restored to $val (read back)"
    else
        say "WARNING: $1 reads back as '$back', expected '$val' - check the Venus setting manually"
    fi
    return 0
}

svc_ctl() {   # svc_ctl -u|-d|-t
    if command -v svc >/dev/null 2>&1; then
        run svc "$1" "$SERVICE_LINK"
    else
        say "svc not available, skipped: svc $1 $SERVICE_LINK"
    fi
}

# set_shadow true|false: edit config.json via python (keeps all other keys)
set_shadow() {
    run "$PY" -c "import json,sys; p=sys.argv[1]; d=json.load(open(p)); d['shadow']=(sys.argv[2]=='true'); json.dump(d,open(p,'w'),indent=2,sort_keys=True)" "$BASE/config.json" "$1"
}
