#!/bin/bash
# Load generator: repeatedly / concurrently hit a JVM stress mode.
#   ./load.sh leak 30 1     -> 30 sequential leak calls
#   ./load.sh gc 200 8      -> 200 gc-churn calls, 8 concurrent
set -uo pipefail
MODE="${1:-leak}"
COUNT="${2:-30}"
CONCURRENCY="${3:-1}"
BASE="http://localhost:8080/jvm-reproducer/jvm"
EXTRA=""
case "$MODE" in
    leak) EXTRA="&mb=10" ;;
    gc)   EXTRA="&mb=50" ;;
esac

echo "Load: mode=$MODE count=$COUNT concurrency=$CONCURRENCY"
run_one() { curl -s "${BASE}?mode=${MODE}${EXTRA}" > /dev/null; }

done_count=0
while [ "$done_count" -lt "$COUNT" ]; do
    for _ in $(seq 1 "$CONCURRENCY"); do run_one & done
    wait
    done_count=$((done_count + CONCURRENCY))
    printf "\r  sent %d/%d" "$done_count" "$COUNT"
done
echo ""
echo "Current status:"
curl -s "${BASE}?mode=status"
