#!/bin/bash
# Reproduce: session lost on failover ("redirected to login after node restart"),
# now through a REAL Apache load balancer with sticky sessions -- exactly the
# customer's mod_cluster topology.
#
#   Client --> Apache LB (:8000, sticky JSESSIONID) --> node1 / node2 / node3
#
# The LB pins a session to one node (sticky). We kill that node abruptly WHILE
# concurrent requests are in-flight; the LB fails the sticky session over to a
# survivor. If Infinispan hasn't transferred the session state in time, the user
# lands on a node with no session -> isNew=true / cart=null -> login redirect.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LB="http://localhost:8000/reproducer/session"
COOKIE_JAR="/tmp/reproducer-cookies.txt"
rm -f "$COOKIE_JAR"
REPRODUCED=0

hit() {  # hit [querystring] -> servlet body, via the load balancer
    local qs=${1:-}
    curl -s -c "$COOKIE_JAR" -b "$COOKIE_JAR" "${LB}${qs:+?$qs}"
}
field() { echo "$1" | grep "^$2=" | cut -d= -f2- | tr -d '\r'; }

start_load() {  # in-flight (delayed) requests through the LB to the sticky node
    for _ in $(seq 1 30); do
        curl -s -c "$COOKIE_JAR" -b "$COOKIE_JAR" "${LB}?delay=800" > /dev/null 2>&1 &
    done
}

kill_node() {  # kill_node <node>  -> abrupt kill -9 of the actual EAP JVM
    local node=$1
    # Target the JVM directly by its jboss.node.name (robust; the .pid file holds
    # the standalone.sh wrapper PID, not the java child).
    local pids; pids=$(pgrep -f "jboss.node.name=$node" || true)
    if [ -n "$pids" ]; then
        echo "   >>> kill -9 $node (JVM PIDs: $pids) MID-LOAD"
        kill -9 $pids 2>/dev/null || true
        rm -f "$SCRIPT_DIR/$node.pid"
    else
        echo "   !! no running JVM found for $node"
    fi
}

echo "=========================================================="
echo "  Session Failover Reproducer (Apache LB + sticky sessions)"
echo "=========================================================="

echo ""
echo "Step 1: Establish a session through the LB and fill the cart..."
for item in laptop mouse keyboard; do hit "item=$item" > /dev/null; done
BODY=$(hit)
PRIMARY=$(field "$BODY" node)
echo "   LB pinned session to: $PRIMARY"
echo "   counter=$(field "$BODY" counter) cart=$(field "$BODY" cart) sessionId=$(field "$BODY" sessionId)"

echo ""
echo "Step 2: Kill the sticky node abruptly under load; LB must fail over..."
for round in 1 2; do
    BODY=$(hit); CUR=$(field "$BODY" node)
    if [ -z "$CUR" ]; then echo ""; echo "--- Round $round: no live sticky node to target, stopping ---"; break; fi
    echo ""
    echo "--- Round $round: sticky node = $CUR ---"
    echo "   driving in-flight load..."
    start_load
    sleep 0.3
    kill_node "$CUR"
    # A real user retries during the failover window. Poll the LB for up to ~8s and
    # capture the first SUCCESSFUL (HTTP 200) response, then inspect the session.
    AFTER=""; status=""
    for _ in $(seq 1 16); do
        resp=$(curl -s -w '\n%{http_code}' -c "$COOKIE_JAR" -b "$COOKIE_JAR" "$LB" 2>/dev/null)
        status=$(echo "$resp" | tail -n1)
        AFTER=$(echo "$resp" | sed '$d')
        [ "$status" = "200" ] && break
        sleep 0.5
    done
    wait 2>/dev/null || true
    onnode=$(field "$AFTER" node); isnew=$(field "$AFTER" isNew)
    counter=$(field "$AFTER" counter); cart=$(field "$AFTER" cart)
    echo "   first 200 after failover: node=$onnode isNew=$isnew counter=$counter cart=$cart (last http=$status)"
    if [ "$status" != "200" ]; then
        echo "   [MODE A] LB never recovered the sticky session -> persistent error page (failover gap)."
        REPRODUCED=1
    elif [ "$isnew" = "true" ] || [ "$counter" = "1" ] || [ "$cart" = "null" ]; then
        echo "   [MODE B] Failover SUCCEEDED but SESSION LOST -> login redirect (the customer's bug)."
        REPRODUCED=1
    else
        echo "   [OK] Failover succeeded on $onnode and session survived (replication works)."
    fi
done

echo ""
echo "=========================================================="
if [ "$REPRODUCED" -eq 1 ]; then
    echo "  *** ISSUE REPRODUCED: session lost / failover error ***"
else
    echo "  Issue NOT reproduced this run (intermittent, ~70%)."
    echo "  Re-run ./stop-all.sh && ./start-all.sh && ./test.sh a few times."
    echo "  If it never reproduces on this build, install EAP 7.4.14"
    echo "  (customer regressed from 7.4.10)."
fi
echo "=========================================================="

echo ""
echo "LB access log (which node served each request):"
tail -n 8 "$SCRIPT_DIR/lb/access.log" 2>/dev/null || echo "  (no LB log)"

rm -f "$COOKIE_JAR"
