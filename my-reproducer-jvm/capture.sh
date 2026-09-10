#!/bin/bash
# Capture the JVM diagnostics that Red Hat support needs for a JVM issue:
# 3 thread dumps (jstack), a live heap dump (jcmd), heap/GC summary. These are
# exactly the artifacts a case must include -- this script produces them for you.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT="$SCRIPT_DIR/diagnostics/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"

# Find the EAP JVM (jboss-modules.jar is the standalone process).
PID="$(pgrep -f 'jboss-modules.jar' | head -1 || true)"
if [ -z "$PID" ]; then echo "No EAP JVM found (is it running via ./run.sh?)"; exit 1; fi
echo "EAP JVM PID: $PID   -> diagnostics in $OUT"

JCMD="$(command -v jcmd || true)"; JSTACK="$(command -v jstack || true)"

# 3 thread dumps, ~3s apart (needed to see which threads are actually busy).
for i in 1 2 3; do
    echo "  thread dump $i/3..."
    if [ -n "$JSTACK" ]; then "$JSTACK" "$PID" > "$OUT/threaddump-$i.txt" 2>&1
    elif [ -n "$JCMD" ]; then "$JCMD" "$PID" Thread.print > "$OUT/threaddump-$i.txt" 2>&1; fi
    sleep 3
done

# Heap summary + live heap dump.
if [ -n "$JCMD" ]; then
    "$JCMD" "$PID" GC.heap_info > "$OUT/heap-info.txt" 2>&1 || true
    echo "  writing live heap dump (may take a few seconds)..."
    "$JCMD" "$PID" GC.heap_dump "$OUT/heap-live.hprof" > "$OUT/heap-dump.log" 2>&1 || true
fi

# Collect any OOM heap dumps + the GC log.
cp "$SCRIPT_DIR"/dumps/*.hprof "$OUT/" 2>/dev/null || true
cp "$SCRIPT_DIR"/gc.log "$OUT/" 2>/dev/null || true

echo ""
echo "Captured:"
ls -lh "$OUT"
echo ""
echo "Analyse with: Eclipse MAT (heap .hprof), 'grep RUNNABLE threaddump-*.txt' (CPU),"
echo "and GCViewer / 'grep Pause gc.log' (GC pauses)."
