#!/bin/sh
# Called from /data/rc.local at every boot: (re)create the daemontools service link.
# /service is rebuilt on boot and after firmware updates; /data survives (VERIFY on v3.70).
HERE="$(cd "$(dirname "$0")" && pwd)"
. "$HERE/lib.sh"
if [ -f "$BASE/disabled" ]; then
    say "disabled flag present ($BASE/disabled) - not starting"
    exit 0
fi
[ -d "$BASE/service" ] || die "service dir missing"
if [ ! -L "$SERVICE_LINK" ]; then
    run ln -s "$BASE/service" "$SERVICE_LINK"
fi
say "service link ok: $SERVICE_LINK"
