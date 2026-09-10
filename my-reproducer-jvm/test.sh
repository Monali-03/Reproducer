#!/bin/bash
# Orchestrate a JVM issue reproduction end to end:
#   show status -> apply stress -> capture diagnostics -> report.
#
#   ./test.sh heap-oom     reproduce heap OutOfMemoryError (+ auto heap dump)
#   ./test.sh leak         slow memory leak (watch heapPctUsed climb)
#   ./test.sh cpu          high CPU (thread dumps show the busy threads)
#   ./test.sh threads      thread exhaustion
#   ./test.sh gc           GC pressure (see gc.log pauses)
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODE="${1:-heap-oom}"
BASE="http://localhost:8080/jvm-reproducer/jvm"

echo "=================================================="
echo "  JVM Reproducer -- mode: $MODE"
echo "=================================================="
echo "Before:"; curl -s "${BASE}?mode=status" | sed 's/^/   /'
echo ""

case "$MODE" in
  heap-oom)
    echo "Triggering heap exhaustion (server has small -Xmx so this is fast)..."
    curl -s "${BASE}?mode=heap-oom" | sed 's/^/   /'
    echo "   -> check dumps/ for the .hprof written on OutOfMemoryError"
    ;;
  leak)
    echo "Leaking memory over 40 calls..."; "$SCRIPT_DIR/load.sh" leak 40 1 | tail -12 | sed 's/^/   /'
    ;;
  gc)
    echo "Churning garbage over 200 calls..."; "$SCRIPT_DIR/load.sh" gc 200 4 | tail -12 | sed 's/^/   /'
    ;;
  cpu)
    echo "Starting CPU burners..."; curl -s "${BASE}?mode=cpu&threads=4&secs=25" | sed 's/^/   /'
    echo "   capturing thread dumps while CPU is hot..."; "$SCRIPT_DIR/capture.sh" | sed 's/^/   /'
    ;;
  threads)
    echo "Creating threads..."; curl -s "${BASE}?mode=threads&count=2000" | sed 's/^/   /'
    ;;
  *) echo "unknown mode: $MODE"; exit 1 ;;
esac

echo ""
echo "After:"; curl -s "${BASE}?mode=status" | sed 's/^/   /' || echo "   (server may have died from OOM -- see server-console.log / dumps/)"
echo ""
if [ "$MODE" != "cpu" ]; then
  echo "Capture full diagnostics (thread dumps + heap dump + gc log): ./capture.sh"
fi
echo "Reset leaked state without restart: curl -s '${BASE}?mode=free'"
