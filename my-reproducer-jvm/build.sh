#!/bin/bash
# Build the JVM reproducer application.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "Building JVM reproducer..."
cd "$SCRIPT_DIR/app"
mvn clean package -DskipTests
echo "Build complete: app/target/jvm-reproducer.war"
