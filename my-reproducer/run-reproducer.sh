#!/bin/bash
# One command to go from "nothing running" to a verdict:
#
#     ./run-reproducer.sh [--repeat N] [--no-terminals]
#
#   1. provisions a compatible JDK (downloads one if the system has none)
#   2. builds the application
#   3. starts every node, each in its own terminal window
#   4. starts the sticky load balancer (if this reproducer bundles one)
#   5. runs the test and reports whether the issue reproduced
#
# The failure being reproduced is intermittent, so --repeat re-runs the whole
# cycle; the test kills nodes, hence each attempt needs a fresh cluster.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/nodes.env"

REPEAT=1
TERMINAL_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --repeat)       REPEAT="${2:?--repeat needs a number}"; shift 2 ;;
        --no-terminals) TERMINAL_ARGS+=(--no-terminals); shift ;;
        -h|--help)      sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

export EAP_HOME="${EAP_HOME:?ERROR: Set EAP_HOME to your JBoss EAP installation}"
eval "$("$SCRIPT_DIR/ensure-jdk.sh" --export)"
export JAVA_HOME

if [ ! -f "$SCRIPT_DIR/app/target/reproducer.war" ]; then
    bash "$SCRIPT_DIR/build.sh"
fi

hits=0
for attempt in $(seq 1 "$REPEAT"); do
    echo ""
    echo "##############  ATTEMPT $attempt / $REPEAT  ##############"
    "$SCRIPT_DIR/stop-cluster.sh" >/dev/null 2>&1 || true
    "$SCRIPT_DIR/start-cluster.sh" "${TERMINAL_ARGS[@]+"${TERMINAL_ARGS[@]}"}" || {
        echo "ERROR: cluster did not start; see the per-node terminals or *.log"; exit 1
    }
    if [ -x "$SCRIPT_DIR/lb/start-lb.sh" ]; then
        # Restart it: a balancer left over from the previous attempt still holds
        # port 8000, and its stale worker state would skew the next run.
        [ -x "$SCRIPT_DIR/lb/stop-lb.sh" ] && "$SCRIPT_DIR/lb/stop-lb.sh" >/dev/null 2>&1
        "$SCRIPT_DIR/lb/start-lb.sh"
    fi

    out="$SCRIPT_DIR/attempt-$attempt.log"
    "$SCRIPT_DIR/test.sh" 2>&1 | tee "$out"
    if grep -q "ISSUE REPRODUCED" "$out"; then
        hits=$((hits + 1))
        echo ">>> reproduced on attempt $attempt (log: $out)"
    fi
done

echo ""
echo "=========================================================="
echo "  Reproduced on $hits of $REPEAT attempt(s)"
if [ "$hits" -eq 0 ]; then
    echo "  The issue did NOT reproduce on this build."
    echo "  That is itself a result: it suggests the bug is not present in"
    echo "  this EAP version. Match the customer's exact micro-version before"
    echo "  concluding, then advise an upgrade if the latest CP is clean."
fi
echo "=========================================================="
