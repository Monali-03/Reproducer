#!/bin/bash
# Bring up the full reproducer environment: 3-node EAP cluster + Apache LB.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$SCRIPT_DIR/start-cluster.sh"
"$SCRIPT_DIR/lb/start-lb.sh"
echo ""
echo "Environment ready. Entry point: http://localhost:8000/reproducer/session"
echo "Run ./test.sh to trigger the failover issue."
