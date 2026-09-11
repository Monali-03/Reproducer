#!/bin/bash
# Run ONE EAP node in the foreground. This is what each terminal window runs,
# and it is also the supported way to add a node by hand:
#
#     ./run-node.sh node2 100 standalone-node2
#
# Each node gets its OWN jboss.server.base.dir. Several standalone instances
# sharing $EAP_HOME/standalone fight over data/, tmp/, log/ and the deployment
# marker files -- a port offset alone is NOT enough to isolate them.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/nodes.env"

NAME="${1:?usage: run-node.sh <node-name> <port-offset> [base-dir-name]}"
OFFSET="${2:?usage: run-node.sh <node-name> <port-offset> [base-dir-name]}"
BASEDIR_NAME="${3:-standalone-$NAME}"

if [ -z "${EAP_HOME:-}" ]; then
    if ! eap_export="$("$SCRIPT_DIR/ensure-eap-home.sh" --export)"; then
        echo "[$NAME] ABORTED: wrong or missing EAP installation (see above)." >&2
        exit 1
    fi
    eval "$eap_export"
fi
export EAP_HOME
BASE="$EAP_HOME/$BASEDIR_NAME"

# A terminal emulator started as a D-Bus service (gnome-terminal, ptyxis) does
# NOT inherit the launcher's environment, so resolve the JDK here too.
if [ -z "${JAVA_HOME:-}" ]; then
    if ! jdk_export="$("$SCRIPT_DIR/ensure-jdk.sh" --export)"; then
        echo "[$NAME] ABORTED: no usable JDK for this EAP (see above)." >&2
        exit 1
    fi
    eval "$jdk_export"
fi
export JAVA_HOME

# Seed a private base dir from the stock one (config only; EAP recreates
# data/, log/ and tmp/ on first boot).
if [ ! -d "$BASE/configuration" ]; then
    echo "[$NAME] creating server base dir $BASE"
    mkdir -p "$BASE/deployments"
    cp -r "$EAP_HOME/standalone/configuration" "$BASE/"
    [ -d "$EAP_HOME/standalone/lib" ] && cp -r "$EAP_HOME/standalone/lib" "$BASE/"
fi

# Deploy the freshly built application.
if [ -f "$SCRIPT_DIR/app/target/reproducer.war" ]; then
    mkdir -p "$BASE/deployments"
    rm -f "$BASE"/deployments/reproducer.war.*
    cp "$SCRIPT_DIR/app/target/reproducer.war" "$BASE/deployments/"
fi

echo "=============================================="
echo "  $NAME  |  offset $OFFSET  |  $BASEDIR_NAME"
echo "  HTTP http://localhost:$((8080 + OFFSET))/reproducer/session"
echo "  mgmt localhost:$((9990 + OFFSET))"
echo "  JDK  $JAVA_HOME"
echo "=============================================="

"$EAP_HOME/bin/standalone.sh" \
    -c "$SERVER_CONFIG" \
    -Djboss.server.base.dir="$BASE" \
    -Djboss.node.name="$NAME" \
    -Djboss.socket.binding.port-offset="$OFFSET" \
    -b 0.0.0.0 2>&1 | tee "$SCRIPT_DIR/$NAME.log"

status=${PIPESTATUS[0]}
echo ""
# Hold the window open only when the server never came up -- a boot failure is
# exactly what you need to read. A node deliberately killed by the failover
# test just closes, so windows don't pile up across repeated attempts.
if grep -q "WFLYSRV0025" "$SCRIPT_DIR/$NAME.log" 2>/dev/null; then
    echo "[$NAME] stopped (status $status)."
    sleep 2
else
    echo "[$NAME] NEVER STARTED (status $status) - the boot errors are above."
    echo "Press Enter to close this window."
    read -r _ || true
fi
