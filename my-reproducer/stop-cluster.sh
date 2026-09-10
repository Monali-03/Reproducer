#!/bin/bash
# Stop every node: graceful :shutdown first, then hard-kill any survivor.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/nodes.env"
EAP_HOME="${EAP_HOME:-}"

for entry in "${NODES[@]}"; do
    read -r name offset http mgmt basedir <<< "$entry"
    if [ -n "$EAP_HOME" ] && [ -f "$EAP_HOME/bin/jboss-cli.sh" ]; then
        "$EAP_HOME/bin/jboss-cli.sh" --connect --controller=localhost:$mgmt \
            --command=":shutdown" >/dev/null 2>&1 && echo "$name: shutdown requested"
    fi
done

sleep 3

# Match on jboss.node.name: the terminal window owns the standalone.sh PID, so
# a recorded PID file is not a reliable handle on the actual JVM.
for entry in "${NODES[@]}"; do
    read -r name offset http mgmt basedir <<< "$entry"
    pids="$(pgrep -f "jboss.node.name=$name" || true)"
    if [ -n "$pids" ]; then
        echo "$name: hard-killing $pids"
        kill -9 $pids 2>/dev/null || true
    fi
    rm -f "$SCRIPT_DIR/$name.pid"
done

echo "All nodes stopped."
