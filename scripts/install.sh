#!/bin/sh
# Install a release on the Cerbo GX in SHADOW mode (publishes nothing to DVCC).
#   sh install.sh [--dry-run]        run from the unpacked release directory
# Steps: checks -> compile -> selftest -> copy release -> config (never overwritten) ->
#        settings backup -> service dir -> boot hook in /data/rc.local -> start.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/.." && pwd)"
. "$HERE/lib.sh"
for a in "$@"; do
    case "$a" in
        --dry-run) DRY_RUN=1 ;;
        *) die "unknown argument $a" ;;
    esac
done

need_root
[ -d "$ROOT/data" ] || die "$ROOT/data not found (not a Venus device?)"
command -v "$PY" >/dev/null 2>&1 || die "python3 not found"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "python >= 3.10 required"

say "compile check"
"$PY" -m compileall -q "$SRC/src/battery_aggregator" >/dev/null || die "compile failed"
say "selftest"
(cd "$SRC/src" && "$PY" -m battery_aggregator.selftest) || die "selftest failed - not installing"

VER="$(cd "$SRC/src" && "$PY" -c 'import battery_aggregator as b; print(b.__version__)')"
REL="$BASE/releases/$VER"
say "installing version $VER to $REL"
run mkdir -p "$BASE/releases" "$BASE/backup" "$BASE/rec" "$LOG_DIR"
if [ -d "$REL" ]; then
    say "release $VER already present - refreshing files"
fi
run mkdir -p "$REL"
run cp -R "$SRC/src/battery_aggregator" "$REL/"
run cp -R "$SRC/scripts" "$REL/"
run cp -R "$SRC/config" "$REL/"
run ln -sfn "$REL" "$BASE/current"

if [ ! -f "$BASE/config.json" ]; then
    say "creating config.json from example (shadow=true)"
    run cp "$SRC/config/config.example.json" "$BASE/config.json"
else
    say "keeping existing config.json"
fi
if [ "$DRY_RUN" != "1" ]; then
    (cd "$REL" && "$PY" -m battery_aggregator.main --check-config --config "$BASE/config.json") || \
        say "config has errors - service will refuse to start until fixed"
fi

TS="$(date +%Y%m%d-%H%M%S)"
say "backing up Venus settings to $BASE/backup/settings-$TS.txt"
if [ "$DRY_RUN" = "1" ]; then
    echo "DRY-RUN: dbus_get $SETTINGS_SVC $SET_BMS / $SET_MON > $BASE/backup/settings-$TS.txt"
else
    {
        echo "BmsInstance=$(dbus_get $SETTINGS_SVC $SET_BMS)"
        echo "BatteryService=$(dbus_get $SETTINGS_SVC $SET_MON)"
    } > "$BASE/backup/settings-$TS.txt"
fi

say "service directory"
run mkdir -p "$BASE/service/log"
run cp "$SRC/scripts/service/run" "$BASE/service/run"
run cp "$SRC/scripts/service/log/run" "$BASE/service/log/run"
run chmod 755 "$BASE/service/run" "$BASE/service/log/run" "$REL/scripts/boot.sh"

if [ -f "$RC_LOCAL" ] && grep -q "$MARK" "$RC_LOCAL"; then
    say "boot hook already in rc.local"
else
    say "adding boot hook to $RC_LOCAL (append only)"
    if [ "$DRY_RUN" = "1" ]; then
        echo "DRY-RUN: append boot hook to $RC_LOCAL"
    else
        [ -f "$RC_LOCAL" ] || { echo "#!/bin/sh" > "$RC_LOCAL"; chmod 755 "$RC_LOCAL"; }
        printf '%s\n[ -x /data/battery-aggregator/current/scripts/boot.sh ] && /data/battery-aggregator/current/scripts/boot.sh &\n' "$MARK" >> "$RC_LOCAL"
    fi
fi

run sh "$REL/scripts/boot.sh"
say "installed $VER in shadow mode. Logs: tail -F $LOG_DIR/current | tai64nlocal"
say "recorder (optional): cd $BASE/current && $PY -m battery_aggregator.tools.recorder --hours 336 &"
