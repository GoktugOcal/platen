#!/bin/sh
set -eu

# SANE backends and manual scanners from PLATEN_* env vars.
python3 -m app.container_setup

# Run the bundled CUPS unless the app should use an external server.
if [ -z "${CUPS_SERVER:-}" ]; then
    mkdir -p /run/cups
    (
        while true; do
            /usr/sbin/cupsd -f || true
            echo "platen: cupsd exited, restarting in 2s" >&2
            sleep 2
        done
    ) &
    i=0
    while [ ! -S /run/cups/cups.sock ] && [ "$i" -lt 100 ]; do
        sleep 0.1
        i=$((i + 1))
    done
    [ -S /run/cups/cups.sock ] || echo "platen: warning: CUPS socket did not appear" >&2
fi

exec uvicorn app.main:create_app --factory \
    --host "${PLATEN_HOST:-0.0.0.0}" --port "${PLATEN_PORT:-8631}" \
    --no-server-header --proxy-headers \
    --forwarded-allow-ips "${PLATEN_TRUSTED_PROXIES:-127.0.0.1}"
