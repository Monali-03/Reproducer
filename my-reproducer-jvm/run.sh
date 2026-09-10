#!/bin/bash
# Start EAP with the JVM reproducer deployed and full JVM diagnostics enabled.
#
# Works for EAP JVM issues directly. For a Red Hat Data Grid *server* JVM issue,
# the SAME JAVA_OPTS below apply -- put them in DATAGRID_HOME/bin/server.conf and
# drive load through Hot Rod / REST instead of this servlet.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EAP_HOME="${EAP_HOME:?Set EAP_HOME to your JBoss EAP installation}"
SERVER_CONFIG="${SERVER_CONFIG:-standalone.xml}"
HEAP="${HEAP:-256m}"           # small on purpose so heap-oom triggers quickly
DUMPS="$SCRIPT_DIR/dumps"
mkdir -p "$DUMPS"

if [ ! -f "$SCRIPT_DIR/app/target/jvm-reproducer.war" ]; then
    bash "$SCRIPT_DIR/build.sh"
fi
echo "Deploying jvm-reproducer.war..."
cp "$SCRIPT_DIR/app/target/jvm-reproducer.war" "$EAP_HOME/standalone/deployments/"

# The diagnostics that make a JVM issue reproducible AND analysable:
#   - HeapDumpOnOutOfMemoryError -> automatic .hprof on OOM (feed to Eclipse MAT)
#   - GC logging                 -> pause times / allocation rate
#   - small MaxMetaspaceSize     -> makes metaspace/classloader leaks surface
export JAVA_OPTS="-Xms${HEAP} -Xmx${HEAP} -XX:MaxMetaspaceSize=256m \
  -XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=${DUMPS} \
  -Xlog:gc*:file=${SCRIPT_DIR}/gc.log:time,uptime,level,tags"

echo "Starting EAP ($SERVER_CONFIG) with -Xmx${HEAP}, heap dumps -> $DUMPS, GC log -> gc.log"
"$EAP_HOME/bin/standalone.sh" -c "$SERVER_CONFIG" -b 0.0.0.0 \
    > "$SCRIPT_DIR/server-console.log" 2>&1 &
echo $! > "$SCRIPT_DIR/eap.pid"

# Wait for readiness
for i in $(seq 1 45); do
    if "$EAP_HOME/bin/jboss-cli.sh" --connect --command=":read-attribute(name=server-state)" 2>/dev/null | grep -q running; then
        echo "EAP running."
        break
    fi
    sleep 2
done

echo ""
echo "Endpoints:"
echo "  status:    http://localhost:8080/jvm-reproducer/jvm?mode=status"
echo "  heap OOM:  http://localhost:8080/jvm-reproducer/jvm?mode=heap-oom"
echo "  leak:      http://localhost:8080/jvm-reproducer/jvm?mode=leak&mb=10"
echo "  high CPU:  http://localhost:8080/jvm-reproducer/jvm?mode=cpu&threads=4&secs=20"
echo "  threads:   http://localhost:8080/jvm-reproducer/jvm?mode=threads&count=1000"
echo ""
echo "Run:  ./test.sh <mode>     Capture diagnostics:  ./capture.sh"
