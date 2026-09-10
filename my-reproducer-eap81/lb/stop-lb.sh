#!/bin/bash
# Stop the httpd load balancer.
set -uo pipefail
LBDIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$LBDIR/httpd.runtime.conf" ]; then
    httpd -f "$LBDIR/httpd.runtime.conf" -k stop 2>/dev/null || true
fi
if [ -f "$LBDIR/httpd.pid" ]; then
    pid=$(cat "$LBDIR/httpd.pid")
    kill "$pid" 2>/dev/null || true
    rm -f "$LBDIR/httpd.pid"
fi
echo "Load balancer stopped."
