#!/bin/bash
# Run the reproducer against EAP (HA clustering mode)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EAP_HOME="${EAP_HOME:?Set EAP_HOME to your JBoss EAP installation}"
SERVER_CONFIG="${SERVER_CONFIG:-standalone-ha.xml}"
NODE_NAME="${NODE_NAME:-node1}"
PORT_OFFSET="${PORT_OFFSET:-0}"

# Deploy the application (hot-deploy on startup)
echo "Deploying reproducer application..."
cp "$SCRIPT_DIR/app/target/reproducer.war" "$EAP_HOME/standalone/deployments/"

# Start EAP with HA configuration
echo "Starting EAP with $SERVER_CONFIG (node=$NODE_NAME, port-offset=$PORT_OFFSET)..."
"$EAP_HOME/bin/standalone.sh" \
    -c "$SERVER_CONFIG" \
    -Djboss.node.name="$NODE_NAME" \
    -Djboss.socket.binding.port-offset="$PORT_OFFSET" \
    -b 0.0.0.0 &
EAP_PID=$!

# Calculate actual HTTP port
HTTP_PORT=$((8080 + PORT_OFFSET))
MGMT_PORT=$((9990 + PORT_OFFSET))

# Wait for EAP to be ready
echo "Waiting for EAP to start on management port $MGMT_PORT..."
TIMEOUT=60
ELAPSED=0
while [ $ELAPSED -lt $TIMEOUT ]; do
    if "$EAP_HOME/bin/jboss-cli.sh" --connect --controller=localhost:$MGMT_PORT \
        --command=":read-attribute(name=server-state)" 2>/dev/null | grep -q "running"; then
        echo "EAP is running."
        break
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
done

if [ $ELAPSED -ge $TIMEOUT ]; then
    echo "ERROR: EAP did not start within ${TIMEOUT}s"
    exit 1
fi

# Apply CLI configuration (server must be running)
if [ -f "$SCRIPT_DIR/local/configure-eap.cli" ]; then
    echo "Applying EAP configuration..."
    "$EAP_HOME/bin/jboss-cli.sh" --connect --controller=localhost:$MGMT_PORT \
        --file="$SCRIPT_DIR/local/configure-eap.cli"
fi

echo ""
echo "EAP started (PID: $EAP_PID)"
echo "Application URL: http://localhost:$HTTP_PORT/reproducer/test"
echo ""
echo "To start a second node for clustering:"
echo "  NODE_NAME=node2 PORT_OFFSET=100 ./run.sh"
