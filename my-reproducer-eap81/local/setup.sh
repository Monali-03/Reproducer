#!/bin/bash
# Setup script for EAP  reproducer
set -euo pipefail

EAP_HOME="${EAP_HOME:?Set EAP_HOME to your JBoss EAP installation}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Applying JBoss CLI configuration..."
"$EAP_HOME/bin/jboss-cli.sh" \
    --connect \
    --file="$SCRIPT_DIR/configure-eap.cli"

echo "Configuration applied successfully."
