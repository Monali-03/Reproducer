#!/bin/bash
# Start the Apache httpd sticky-session load balancer in front of the EAP nodes.
set -euo pipefail
LBDIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$LBDIR/runtime"

# Materialize a config with absolute paths.
sed "s#__LBDIR__#$LBDIR#g" "$LBDIR/httpd.conf" > "$LBDIR/httpd.runtime.conf"

# Validate config first (fails loudly instead of half-starting).
httpd -f "$LBDIR/httpd.runtime.conf" -t

echo "Starting httpd load balancer on http://localhost:8000 ..."
httpd -f "$LBDIR/httpd.runtime.conf" -k start
sleep 1
if [ -f "$LBDIR/httpd.pid" ] && kill -0 "$(cat "$LBDIR/httpd.pid")" 2>/dev/null; then
    echo "Load balancer RUNNING (PID $(cat "$LBDIR/httpd.pid"))"
    echo "  App via LB:        http://localhost:8000/reproducer/session"
    echo "  Balancer manager:  http://localhost:8000/balancer-manager"
else
    echo "ERROR: load balancer failed to start. See $LBDIR/error.log"; exit 1
fi
