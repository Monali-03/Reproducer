#!/bin/bash
# Start the 3-node EAP cluster for reproducing: session lost on failover
#
# Each node comes up in ITS OWN TERMINAL WINDOW, so you can watch the servers
# boot the way you would in a real lab. Pass --no-terminals to keep them in the
# background writing <node>.log instead (use that over SSH or in CI).
#
# A missing or incompatible JDK is not your problem to solve: ensure-jdk.sh
# picks the right one for this EAP release and downloads it if the machine
# hasn't got one.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/nodes.env"
EAP_HOME="${EAP_HOME:?ERROR: Set EAP_HOME to your JBoss EAP installation}"

USE_TERMINALS=1
[ "${1:-}" = "--no-terminals" ] && USE_TERMINALS=0

if [ ! -f "$EAP_HOME/bin/standalone.sh" ]; then
    echo "ERROR: $EAP_HOME/bin/standalone.sh not found"
    exit 1
fi

eval "$("$SCRIPT_DIR/ensure-jdk.sh" --export)"
export JAVA_HOME EAP_HOME

if [ ! -f "$SCRIPT_DIR/app/target/reproducer.war" ]; then
    echo "Application not built. Running build.sh first..."
    bash "$SCRIPT_DIR/build.sh"
fi

# --- where do the node windows come from? ----------------------------------
detect_terminal() {
    local t
    for t in ptyxis gnome-terminal konsole xfce4-terminal kitty alacritty xterm tmux; do
        command -v "$t" >/dev/null 2>&1 && { echo "$t"; return; }
    done
    echo none
}
TERM_EMU="$(detect_terminal)"
if [ "$USE_TERMINALS" = 1 ]; then
    if [ "$TERM_EMU" = none ]; then
        echo "No terminal emulator found - starting nodes in the background instead."
        USE_TERMINALS=0
    elif [ "$TERM_EMU" != tmux ] && [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
        echo "No graphical display - starting nodes in the background instead."
        USE_TERMINALS=0
    fi
fi

launch_node() {  # launch_node <name> <offset> <base-dir-name>
    local name=$1 offset=$2 basedir=$3 cmd
    # gnome-terminal and ptyxis run as D-Bus services: a window they open does
    # NOT inherit this shell's environment, so hand the settings over on the
    # command line rather than exporting and hoping.
    cmd="EAP_HOME=$(printf '%q' "$EAP_HOME") JAVA_HOME=$(printf '%q' "$JAVA_HOME")"
    cmd="$cmd $(printf '%q' "$SCRIPT_DIR/run-node.sh") $name $offset $basedir"

    if [ "$USE_TERMINALS" = 0 ]; then
        nohup bash -c "$cmd" >/dev/null 2>&1 &
        echo "  $name -> background (log: $name.log)"
        return
    fi
    case "$TERM_EMU" in
        ptyxis)         setsid ptyxis --new-window --title "$name" -- bash -c "$cmd" >/dev/null 2>&1 & ;;
        gnome-terminal) setsid gnome-terminal --title="$name" -- bash -c "$cmd" >/dev/null 2>&1 & ;;
        konsole)        setsid konsole -p tabtitle="$name" -e bash -c "$cmd" >/dev/null 2>&1 & ;;
        xfce4-terminal) setsid xfce4-terminal --title="$name" -x bash -c "$cmd" >/dev/null 2>&1 & ;;
        kitty)          setsid kitty --title "$name" bash -c "$cmd" >/dev/null 2>&1 & ;;
        alacritty)      setsid alacritty --title "$name" -e bash -c "$cmd" >/dev/null 2>&1 & ;;
        xterm)          setsid xterm -T "$name" -e bash -c "$cmd" >/dev/null 2>&1 & ;;
        tmux)           tmux new-session -d -s "repro-$name" "bash -c $(printf '%q' "$cmd")" ;;
    esac
    echo "  $name -> $TERM_EMU window (also logged to $name.log)"
}

wait_for_node() {
    local name=$1 mgmt_port=$2 timeout=180 elapsed=0
    echo "Waiting for $name (mgmt port $mgmt_port)..."
    while [ $elapsed -lt $timeout ]; do
        if "$EAP_HOME/bin/jboss-cli.sh" --connect --controller=localhost:$mgmt_port \
            --command=":read-attribute(name=server-state)" 2>/dev/null | grep -q "running"; then
            echo "$name is RUNNING"
            return 0
        fi
        sleep 2; elapsed=$((elapsed + 2))
    done
    echo "ERROR: $name did not start within ${timeout}s - check its window or $name.log"
    return 1
}

apply_cli() {
    local mgmt_port=$1 f
    for f in configure-eap.cli instance-id.cli local/instance-id.cli; do
        if [ -f "$SCRIPT_DIR/$f" ]; then
            "$EAP_HOME/bin/jboss-cli.sh" --connect --controller=localhost:$mgmt_port \
                --file="$SCRIPT_DIR/$f" >/dev/null 2>&1 || true
        fi
    done
}

echo "Launching 3 nodes..."
for entry in "${NODES[@]}"; do
    read -r name offset http mgmt basedir <<< "$entry"
    launch_node "$name" "$offset" "$basedir"
done

for entry in "${NODES[@]}"; do
    read -r name offset http mgmt basedir <<< "$entry"
    wait_for_node "$name" "$mgmt"
done

# instance-id gives JSESSIONID its node route suffix (sticky LB); it triggers a
# :reload, so wait for every node to come back afterwards.
for entry in "${NODES[@]}"; do
    read -r name offset http mgmt basedir <<< "$entry"
    echo "Applying CLI configuration to $name..."
    apply_cli "$mgmt"
done
for entry in "${NODES[@]}"; do
    read -r name offset http mgmt basedir <<< "$entry"
    sleep 2
    wait_for_node "$name" "$mgmt"
done

echo ""
echo "Cluster membership (web cache-container, from node1):"
"$EAP_HOME/bin/jboss-cli.sh" --connect --controller=localhost:9990 \
    --command="/subsystem=infinispan/cache-container=web:read-resource(include-runtime=true)" \
    2>/dev/null | grep -E "coordinator" || echo "  (could not read membership)"

echo ""
echo "=========================================="
echo "  3-node cluster started"
echo "=========================================="
for entry in "${NODES[@]}"; do
    read -r name offset http mgmt basedir <<< "$entry"
    echo "  $name: http://localhost:$http/reproducer/session"
done
echo ""
echo "Next: start the sticky load balancer, then run the test:"
echo "  ./lb/start-lb.sh    # Apache sticky-session LB on :8000"
echo "  ./test.sh           # drive traffic through the LB + fail a node"
echo "  ./run-reproducer.sh --repeat 5   # do all of it, repeatedly"
echo "Run ./stop-cluster.sh to stop all nodes"
