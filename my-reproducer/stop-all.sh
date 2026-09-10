#!/bin/bash
# Tear down the full reproducer environment: Apache LB + EAP cluster.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
"$SCRIPT_DIR/lb/stop-lb.sh" || true
"$SCRIPT_DIR/stop-cluster.sh" || true
echo "Environment stopped."
