#!/bin/bash
# Build the reproducer application
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Building reproducer application..."
cd "$SCRIPT_DIR/app"
mvn clean package -DskipTests
echo "Build complete: app/target/reproducer.war"
