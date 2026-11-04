#!/bin/sh
# Thin wrapper: run the installed Mouser binary's lifecycle CLI.
#
#   mouser-ctl.sh status|stop|start|restart|assert-single
#
# Exit codes for `status`: 0 = not running, 1 = one instance, 2 = many.
# MOUSER_INSTALL_DIR overrides the /Applications install root.
set -eu

root="${MOUSER_INSTALL_DIR:-/Applications}"
exe="$root/Mouser.app/Contents/MacOS/Mouser"

if [ ! -x "$exe" ]; then
    echo "mouser-ctl: installed binary not found: $exe" >&2
    exit 66
fi

exec "$exe" --ctl "$@"
