"""Reproducer package generator — creates complete, issue-specific reproducer packages."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .llm_client import LLMClient
from .models import IssueAnalysis, ReproducerConfig


_GENERATOR_SYSTEM_PROMPT = """\
You are an expert Red Hat middleware engineer creating minimal reproducer
packages for customer-reported issues.

Rules you MUST follow:
1. EAP 7.x uses javax.* namespace.  EAP 8.x uses jakarta.* namespace.
2. Produce MINIMAL reproducers -- only the code and config needed to
   demonstrate the bug.
3. NEVER include customer secrets, passwords, hostnames.
4. All generated code must be COMPLETE and WORKING.
5. For clustering issues, use standalone-ha.xml or standalone-full-ha.xml.
6. Include numbered reproduction steps.
7. Shell scripts must use set -euo pipefail and be well-commented.
8. For multi-node setups, generate scripts that start ALL required nodes
   with correct port offsets and node names.
9. Generate test scripts that ACTUALLY trigger the reported issue
   (concurrent requests, failover, node restart, etc).
10. Match the customer's topology as closely as possible.

Return a JSON object where keys are file paths and values are file contents.
"""


# Emitted verbatim into every reproducer package. Kept as module constants
# because they are long, static shell programs -- there is nothing to
# interpolate except the placeholders marked __LIKE_THIS__.

_ENSURE_SERVER_HOME_SH = """\
#!/bin/bash
# Resolve the server installation this reproducer is FOR, and refuse to run
# against any other one. Prints "export SERVER_HOME=... EAP_HOME=..." on stdout,
# progress on stderr:
#
#     eval "$(./ensure-server-home.sh --export)"
#
# Why this exists: the target product/version comes from the support case, but
# the install path comes from whoever's shell happens to be running the script.
# Nothing used to connect the two, so a reproducer generated from an EAP 8 case
# would silently deploy onto an EAP 7 server (jakarta app on a javax server: the
# war deploys "OK" and every servlet 404s). The case file decides.
#
# Resolution order:
#   1. A product-specific variable ($HOME_VARS, e.g. EAP8_HOME / DATAGRID_HOME).
#      Setting one of these is an explicit statement of intent -- a version
#      mismatch there is a hard error.
#   2. A generic variable (EAP_HOME / JBOSS_HOME) exported from ~/.bashrc. This
#      is ambient, not intent: if it points at the wrong major version it is
#      skipped with a note rather than aborting the run.
#   3. Autodiscovery under the usual lab locations.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/nodes.env"

log() { echo "[server-home] $*" >&2; }

# What proves a directory is an installation of this product, and where its
# version string lives.
case "$TARGET_KIND" in
    datagrid) MARKER="bin/server.sh" ;;
    *)        MARKER="bin/standalone.sh" ;;
esac

installed_version() {  # -> 7.4.23.GA / 8.5.2.GA
    # EAP: "7.4.23.GA". Data Grid: "Red Hat Data Grid - Version 8.5.2.GA".
    # Take the last version-shaped token on the first non-empty line.
    grep -oE '[0-9]+\\.[0-9]+[0-9.]*(\\.GA|\\.CR[0-9]+|\\.Final)?' \
        "$1/version.txt" 2>/dev/null | tail -1
}

find_installs() {
    local root s
    for root in ${EAP_SEARCH_PATHS:-} "$HOME/Documents/EAP_lab" "$HOME/Documents/Datagrid" \
                "$HOME/EAP" "$HOME/eap" "$HOME/jboss" "$HOME/Downloads" \
                /opt/jboss /opt/eap /opt/datagrid /opt; do
        [ -d "$root" ] || continue
        while IFS= read -r s; do
            [ -n "$s" ] && dirname "$(dirname "$s")"
        done < <(find "$root" -maxdepth 4 -path "*/$MARKER" 2>/dev/null)
    done | sort -u
}

report_candidates() {
    local h v found=0
    while IFS= read -r h; do
        [ -n "$h" ] || continue
        v="$(installed_version "$h")"
        log "    ${v:-unknown}  $h"
        found=1
    done < <(find_installs)
    [ "$found" = 1 ] || log "    (none found)"
}

version_matches() {  # version_matches <installed>
    [ "${1%%.*}" = "$TARGET_MAJOR" ]
}

SERVER_HOME=""; HAVE=""; STRICT=0

# --- 1 + 2: variables from the environment ---------------------------------
for var in $HOME_VARS; do
    val="${!var:-}"
    [ -n "$val" ] || continue
    # Product-specific names (EAP8_HOME, DATAGRID_HOME) are intent; the bare
    # EAP_HOME / JBOSS_HOME set once in ~/.bashrc is not.
    case "$var" in
        EAP_HOME|JBOSS_HOME|SERVER_HOME) explicit=0 ;;
        *) explicit=1 ;;
    esac

    if [ ! -f "$val/$MARKER" ]; then
        if [ "$explicit" = 1 ]; then
            log "ERROR: \\$$var=$val has no $MARKER"
            exit 1
        fi
        log "ignoring \\$$var=$val (no $MARKER -- not a $TARGET_PRODUCT install)"
        continue
    fi

    v="$(installed_version "$val")"
    if version_matches "$v"; then
        SERVER_HOME="$val"; HAVE="$v"
        log "using \\$$var -> ${v:-unknown} at $val"
        break
    fi

    if [ "$explicit" = 1 ]; then
        log "ERROR: this reproducer targets $TARGET_PRODUCT $TARGET_VERSION (from the"
        log "       support case), but \\$$var points at ${v:-an unknown version}:"
        log "         $val"
        log "       Running it there would prove nothing. Installations found:"
        report_candidates
        exit 1
    fi
    log "\\$$var points at $v, but this reproducer targets $TARGET_PRODUCT $TARGET_VERSION."
    log "  Ignoring it and looking for a ${TARGET_MAJOR}.x install instead."
done

# --- 3: autodiscovery -------------------------------------------------------
if [ -z "$SERVER_HOME" ]; then
    CHOSEN=""; FALLBACK=""
    while IFS= read -r h; do
        [ -n "$h" ] || continue
        v="$(installed_version "$h")"
        version_matches "$v" || continue
        case "$v" in
            "$TARGET_VERSION"*) CHOSEN="$h"; break ;;   # exact case version wins
        esac
        [ -z "$FALLBACK" ] && FALLBACK="$h"
    done < <(find_installs)
    SERVER_HOME="${CHOSEN:-$FALLBACK}"
    if [ -z "$SERVER_HOME" ]; then
        log "ERROR: no $TARGET_PRODUCT ${TARGET_MAJOR}.x installation found."
        log "       This reproducer targets $TARGET_PRODUCT $TARGET_VERSION."
        log "       Installations found on this machine:"
        report_candidates
        log "       Set one of: $HOME_VARS -- or point EAP_SEARCH_PATHS at the"
        log "       directory holding the release."
        exit 1
    fi
    HAVE="$(installed_version "$SERVER_HOME")"
    log "auto-selected ${HAVE:-unknown} at $SERVER_HOME"
fi

# Same major, different micro: allowed, but say so loudly. Version-specific
# regressions are exactly the kind that will not reproduce on the wrong CP.
case "$HAVE" in
    "$TARGET_VERSION"*) log "version matches the case exactly ($HAVE)" ;;
    *) log "WARNING: the case says $TARGET_VERSION but this install is ${HAVE:-unknown}."
       log "         If the issue is a regression tied to a specific cumulative"
       log "         patch, it may not reproduce here. Apply the matching CP to"
       log "         be certain of a negative result." ;;
esac

if [ "${1:-}" = "--export" ]; then
    printf 'export SERVER_HOME=%s\n' "$SERVER_HOME"
    # EAP_HOME stays exported for the EAP scripts and for ensure-jdk.sh.
    printf 'export EAP_HOME=%s\n' "$SERVER_HOME"
fi
"""


_ENSURE_JDK_SH = """\
#!/bin/bash
# Resolve -- and if necessary DOWNLOAD -- a JDK that can actually boot the EAP
# installed at $EAP_HOME. Prints "export JAVA_HOME=..." on stdout; all progress
# goes to stderr, so callers can do:
#
#     eval "$(./ensure-jdk.sh --export)"
#
# Why this exists: current distros ship only JDK 21/25, and neither EAP release
# boots on those out of the box.
#   * EAP 7.4 - stock standalone*.xml still contain the legacy `security`
#     subsystem and <security-realms>. Both are REJECTED on JDK 14+
#     (WFLYSEC0106 / WFLYDM0145) and the server dies at boot (WFLYSRV0056)
#     before a single deployment starts. Needs JDK 8 or 11.
#   * EAP 8.x - Jakarta EE 10, needs JDK 17 or 21; will not run on 11.
set -euo pipefail

JDKS_DIR="${REPRODUCER_JDKS_DIR:-$HOME/jdks}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# nodes.env (EAP/Data Grid) or jvm.env (JVM-only) tells us what we are serving.
# Note the `if`: under `set -e` a bare `[ -f x ] && source x` aborts the whole
# script when the file is absent, because the AND-list itself returns 1.
for env_file in nodes.env jvm.env; do
    if [ -f "$SCRIPT_DIR/$env_file" ]; then
        source "$SCRIPT_DIR/$env_file"
    fi
done
TARGET_KIND="${TARGET_KIND:-eap}"

log() { echo "[ensure-jdk] $*" >&2; }

java_major() {  # java_major <path-to-java> -> 8, 11, 17, 21, ...
    "$1" -version 2>&1 | head -1 \\
        | sed -e 's/.*version "\\([0-9]*\\)\\.\\([0-9]*\\).*/\\1 \\2/' \\
        | awk '{ if ($1 == 1) print $2; else print $1 }'
}

# --- Which JDK does THIS product need? -------------------------------------
SERVER="${SERVER_HOME:-${EAP_HOME:-}}"
case "$TARGET_KIND" in
jvm)
    # No server: honour REQUIRED_JDK from jvm.env, else whatever is current.
    JDK_MIN="${REQUIRED_JDK_MIN:-8}"; JDK_MAX="${REQUIRED_JDK_MAX:-25}"
    JDK_WANT="${REQUIRED_JDK:-17}"
    REASON="JVM reproducer: using JDK ${JDK_WANT} (set REQUIRED_JDK to match the customer)"
    ;;
datagrid)
    DG_VER="$(grep -oE '[0-9]+\\.[0-9]+' "$SERVER/version.txt" 2>/dev/null | tail -1)"
    JDK_MIN=17; JDK_MAX=21; JDK_WANT=17
    REASON="Red Hat Data Grid ${DG_VER:-8.x}: server requires JDK 17 or 21"
    ;;
*)
    EAP_VER="$(grep -oE '[0-9]+\\.[0-9]+' "$SERVER/version.txt" 2>/dev/null | head -1)"
    case "${EAP_VER%%.*}" in
        7)  JDK_MIN=8;  JDK_MAX=11; JDK_WANT=11
            REASON="EAP $EAP_VER: legacy security subsystem/realms are rejected on JDK 14+" ;;
        8)  JDK_MIN=17; JDK_MAX=21; JDK_WANT=17
            REASON="EAP $EAP_VER: Jakarta EE 10 requires JDK 17 or 21" ;;
        *)  JDK_MIN=17; JDK_MAX=21; JDK_WANT=17
            REASON="unknown EAP version at $SERVER; assuming a modern release" ;;
    esac
    ;;
esac
log "$REASON -> need JDK ${JDK_MIN}..${JDK_MAX}"

# --- Is a usable JDK already on this machine? ------------------------------
candidates=()
[ -n "${JAVA_HOME:-}" ] && candidates+=("$JAVA_HOME")
for d in "$JDKS_DIR"/*/ /usr/lib/jvm/*/; do
    [ -d "$d" ] && candidates+=("${d%/}")
done
sys_java="$(command -v java || true)"
if [ -n "$sys_java" ]; then
    candidates+=("$(dirname "$(dirname "$(readlink -f "$sys_java")")")")
fi

pick_jdk() {
    local exact="" any="" c m
    for c in "${candidates[@]+"${candidates[@]}"}"; do
        [ -x "$c/bin/java" ] || continue
        m="$(java_major "$c/bin/java" 2>/dev/null || true)"
        [[ "$m" =~ ^[0-9]+$ ]] || continue
        if [ "$m" -ge "$JDK_MIN" ] && [ "$m" -le "$JDK_MAX" ]; then
            # Prefer the exact recommended version, else anything in range.
            if [ "$m" -eq "$JDK_WANT" ]; then exact="$c"; break; fi
            [ -z "$any" ] && any="$c"
        fi
    done
    echo "${exact:-$any}"
}

RESOLVED="$(pick_jdk)"

# --- Nothing suitable: fetch a Temurin build into $JDKS_DIR ----------------
if [ -z "$RESOLVED" ]; then
    log "no JDK in ${JDK_MIN}..${JDK_MAX} found on this system - downloading Temurin ${JDK_WANT}"
    case "$(uname -m)" in
        x86_64)          ARCH=x64 ;;
        aarch64|arm64)   ARCH=aarch64 ;;
        *) log "ERROR: unsupported CPU architecture $(uname -m)"; exit 1 ;;
    esac
    URL="https://api.adoptium.net/v3/binary/latest/${JDK_WANT}/ga/linux/${ARCH}/jdk/hotspot/normal/eclipse"
    mkdir -p "$JDKS_DIR"
    TARBALL="$(mktemp "${TMPDIR:-/tmp}/temurin-${JDK_WANT}-XXXXXX.tar.gz")"
    trap 'rm -f "$TARBALL"' EXIT
    if ! curl -fL --retry 3 --connect-timeout 20 -o "$TARBALL" "$URL"; then
        log "ERROR: download failed ($URL)."
        log "       No internet access? Install a JDK ${JDK_MIN}-${JDK_MAX} manually and"
        log "       re-run with JAVA_HOME pointing at it."
        exit 1
    fi
    tar -xzf "$TARBALL" -C "$JDKS_DIR"
    log "extracted into $JDKS_DIR"

    # Re-scan: the new JDK is now the only fresh directory under $JDKS_DIR.
    candidates=()
    for d in "$JDKS_DIR"/*/; do [ -d "$d" ] && candidates+=("${d%/}"); done
    RESOLVED="$(pick_jdk)"
    if [ -z "$RESOLVED" ]; then
        log "ERROR: downloaded JDK ${JDK_WANT} but could not validate it under $JDKS_DIR"
        exit 1
    fi
fi

MAJOR="$(java_major "$RESOLVED/bin/java")"
log "using JDK $MAJOR at $RESOLVED"

# Persist next to this script so every other script/terminal agrees.
ENV_FILE="$(cd "$(dirname "$0")" && pwd)/.jdk-env"
printf 'export JAVA_HOME=%s\\n' "$RESOLVED" > "$ENV_FILE"

if [ "${1:-}" = "--export" ]; then
    printf 'export JAVA_HOME=%s\\n' "$RESOLVED"
fi
"""


# --- Red Hat Data Grid -----------------------------------------------------
# Data Grid Server is not EAP: bin/server.sh instead of bin/standalone.sh, one
# endpoint port instead of http+management, `-s <server-root>` instead of
# jboss.server.base.dir, and endpoint security is ON by default so every node
# needs a user created before anything can talk to it.

_DG_RUN_NODE_SH = """\
#!/bin/bash
# Run ONE Data Grid node in this terminal:  ./run-node.sh <name> <offset> <root>
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/nodes.env"

NAME="${1:?usage: run-node.sh <name> <port-offset> [server-root-name]}"
OFFSET="${2:?usage: run-node.sh <name> <port-offset> [server-root-name]}"
ROOT_NAME="${3:-server-$NAME}"

if [ -z "${SERVER_HOME:-}" ]; then
    if ! home_export="$("$SCRIPT_DIR/ensure-server-home.sh" --export)"; then
        echo "[$NAME] ABORTED: wrong or missing Data Grid installation (see above)." >&2
        exit 1
    fi
    eval "$home_export"
fi
export SERVER_HOME
if ! jdk_export="$("$SCRIPT_DIR/ensure-jdk.sh" --export)"; then
    echo "[$NAME] ABORTED: no usable JDK (see above)." >&2
    exit 1
fi
eval "$jdk_export"
export JAVA_HOME

ROOT="$SERVER_HOME/$ROOT_NAME"

# Each node needs its own server root. Sharing one means they fight over
# data/, log/ and the index files -- a port offset alone does not isolate them.
if [ ! -d "$ROOT/conf" ]; then
    echo "[$NAME] seeding server root $ROOT from the stock server/ directory"
    mkdir -p "$ROOT"
    cp -r "$SERVER_HOME/server/conf" "$ROOT/conf"
fi

# Endpoints are authenticated by default; without a user every REST call is 401
# and the reproducer looks broken when it is only locked.
if [ ! -s "$ROOT/conf/users.properties" ] || \\
   ! grep -q "^${DG_USER}=" "$ROOT/conf/users.properties" 2>/dev/null; then
    echo "[$NAME] creating endpoint user '$DG_USER'"
    "$SERVER_HOME/bin/cli.sh" user create "$DG_USER" -p "$DG_PASS" -g admin \\
        -s "$ROOT" >/dev/null 2>&1 || \\
        echo "[$NAME] WARNING: could not create user; REST calls may return 401"
fi

# The stock `tcp` stack discovers peers with MPING, i.e. IP multicast. On a
# laptop that usually finds nothing -- loopback has no MULTICAST flag and the
# host firewall drops the datagrams on the real NIC -- so every node forms its
# own one-member cluster and the reproducer quietly tests nothing. Layer a
# TCPPING stack with the peers listed explicitly over the stock config
# (`-c` is repeatable and the files are merged) so the cluster is deterministic.
#
# The schema namespace is read out of the install's own infinispan.xml rather
# than hardcoded, so this works on whatever Data Grid version is present.
NS="$(grep -oE 'urn:infinispan:config:[0-9]+\\.[0-9]+' \\
      "$SERVER_HOME/server/conf/infinispan.xml" | head -1)"
if [ -z "$NS" ]; then
    echo "[$NAME] WARNING: could not read the config namespace; using multicast discovery."
    OVERLAY=()
else
    hosts=""
    for entry in "${NODES[@]}"; do
        read -r _ o _ _ <<< "$entry"
        hosts="${hosts:+$hosts,}127.0.0.1[$((7800 + o))]"
    done
    cat > "$ROOT/conf/repro-cluster.xml" <<XML
<infinispan xmlns="$NS">
   <jgroups>
      <stack name="repro-tcp" extends="tcp">
         <TCPPING initial_hosts="$hosts" port_range="0"
                  stack.combine="REPLACE" stack.position="MPING"/>
      </stack>
   </jgroups>
   <cache-container name="default" statistics="true">
      <transport cluster="repro-cluster" stack="repro-tcp"
                 node-name="\\${infinispan.node.name:}"/>
      <security>
         <authorization/>
      </security>
   </cache-container>
</infinispan>
XML
    OVERLAY=(-c repro-cluster.xml)
    echo "[$NAME] JGroups discovery: TCPPING $hosts"
fi

echo "[$NAME] Data Grid: port $((11222 + OFFSET)), root $ROOT"
echo "[$NAME] JAVA_HOME=$JAVA_HOME"
echo

set +e
"$SERVER_HOME/bin/server.sh" -o "$OFFSET" -n "$NAME" -s "$ROOT" \\
    -c "$SERVER_CONFIG" ${OVERLAY[@]+"${OVERLAY[@]}"} \\
    -b "$BIND_ADDRESS" -k "$BIND_ADDRESS" 2>&1 | tee "$SCRIPT_DIR/$NAME.log"
status=${PIPESTATUS[0]}
set -e

if grep -q "ISPN080001\\|ISPN080034" "$SCRIPT_DIR/$NAME.log" 2>/dev/null; then
    echo "[$NAME] stopped (status $status)."
    sleep 2
else
    echo "[$NAME] NEVER STARTED (status $status) - the boot errors are above."
    echo "Press Enter to close this window."
    read -r _ || true
fi
"""


_DG_START_CLUSTER_SH = """\
#!/bin/bash
# Start every Data Grid node, each in its own terminal window.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/nodes.env"

USE_TERMINALS=1
[ "${1:-}" = "--no-terminals" ] && USE_TERMINALS=0

# `eval "$(cmd)"` hides cmd's exit status from set -e, so capture and check first.
resolve() {
    local out
    if ! out="$("$SCRIPT_DIR/$1" --export)"; then
        echo "ABORTED: $1 refused to run this reproducer here (see above)." >&2
        exit 1
    fi
    eval "$out"
}
resolve ensure-server-home.sh
resolve ensure-jdk.sh
export SERVER_HOME JAVA_HOME

detect_terminal() {
    for t in ptyxis gnome-terminal konsole xfce4-terminal kitty alacritty xterm; do
        command -v "$t" >/dev/null 2>&1 && { echo "$t"; return; }
    done
    command -v tmux >/dev/null 2>&1 && { echo tmux; return; }
    echo none
}

launch_node() {
    local name="$1" offset="$2" root="$3"
    # Terminals started over D-Bus (ptyxis, gnome-terminal) do NOT inherit this
    # shell's environment, so everything needed is passed on the command line.
    local cmd
    cmd="SERVER_HOME=$(printf '%q' "$SERVER_HOME") JAVA_HOME=$(printf '%q' "$JAVA_HOME")"
    cmd="$cmd $(printf '%q' "$SCRIPT_DIR/run-node.sh") $name $offset $root"

    case "$TERM_EMU" in
        ptyxis)         ptyxis --new-window -- bash -lc "$cmd" & ;;
        gnome-terminal) gnome-terminal --window -- bash -lc "$cmd" & ;;
        konsole)        konsole --new-tab -e bash -lc "$cmd" & ;;
        xfce4-terminal) xfce4-terminal --window -e "bash -lc '$cmd'" & ;;
        kitty)          kitty bash -lc "$cmd" & ;;
        alacritty)      alacritty -e bash -lc "$cmd" & ;;
        xterm)          xterm -e bash -lc "$cmd" & ;;
        tmux)           tmux new-session -d -s "repro-$name" "bash -lc '$cmd'" ;;
        *)              bash -c "$cmd" > "$SCRIPT_DIR/$name.log" 2>&1 & ;;
    esac
}

TERM_EMU=none
[ "$USE_TERMINALS" = 1 ] && TERM_EMU="$(detect_terminal)"
[ "$TERM_EMU" = none ] && echo "No terminal emulator; running nodes in the background."

echo "Launching ${#NODES[@]} Data Grid nodes..."
for entry in "${NODES[@]}"; do
    read -r name offset port root <<< "$entry"
    launch_node "$name" "$offset" "$root"
    echo "  $name -> port $port (log: $name.log)"
    sleep 3
done

# Readiness: the REST health endpoint, not just an open socket -- a listening
# port says the JVM is up, not that the cache manager is.
for entry in "${NODES[@]}"; do
    read -r name offset port root <<< "$entry"
    echo -n "Waiting for $name (port $port)..."
    for i in $(seq 1 60); do
        # DIGEST, not Basic. The properties realm stores only hashed
        # credentials, so Basic is rejected with ISPN080052 -- and the
        # health endpoint answers anonymously, so probing it with the
        # wrong scheme reports HEALTHY while every real call is 403.
        code="$(curl -s -o /dev/null -w '%{http_code}' --digest -u "$DG_USER:$DG_PASS" \\
                "http://localhost:$port/rest/v2/caches" 2>/dev/null || true)"
        if [ "$code" = "200" ]; then echo " HEALTHY"; break; fi
        [ "$i" = 60 ] && { echo " TIMED OUT (last HTTP $code) - see $name.log"; exit 1; }
        sleep 2
    done
done

echo
echo "Cluster membership (from ${NODES[0]%% *}):"
read -r _ _ first_port _ <<< "${NODES[0]}"
members="$(curl -s --digest -u "$DG_USER:$DG_PASS" \\
    "http://localhost:$first_port/rest/v2/cache-managers/default" \\
    | tr ',' '\\n' | grep -iE 'cluster_name|cluster_size|cluster_members')"
if [ -n "$members" ]; then
    echo "$members" | sed 's/^/  /'
else
    echo "  (could not read cluster info)"
fi

echo
echo "=========================================="
echo "  ${#NODES[@]}-node Data Grid cluster started"
echo "=========================================="
for entry in "${NODES[@]}"; do
    read -r name offset port root <<< "$entry"
    echo "  $name: http://localhost:$port  (console: /console)"
done
echo
echo "Next: ./test.sh   or   ./run-reproducer.sh --repeat 5"
echo "Run ./stop-cluster.sh to stop all nodes"
"""


_DG_STOP_CLUSTER_SH = """\
#!/bin/bash
# Stop every Data Grid node started by this reproducer.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/nodes.env"

# Match the server's own argv (-n <name> -s <root>) and the run-node.sh
# wrapper. Matching only the wrapper leaves the JVM running and holding the
# port, which makes the next attempt fail for a reason that has nothing to
# do with the customer's issue.
node_pids() {
    local name="$1" root="$2"
    { pgrep -f -- "-n $name -s .*$root" || true
      pgrep -f -- "run-node.sh $name " || true; } | sort -u
}

for entry in "${NODES[@]}"; do
    read -r name offset port root <<< "$entry"
    pids="$(node_pids "$name" "$root")"
    if [ -n "$pids" ]; then
        echo "$name: stopping ($(echo $pids | tr '\\n' ' '))"
        kill $pids 2>/dev/null || true
    fi
done

sleep 3
for entry in "${NODES[@]}"; do
    read -r name offset port root <<< "$entry"
    pids="$(node_pids "$name" "$root")"
    [ -n "$pids" ] && kill -9 $pids 2>/dev/null || true
done

# Do not return until the ports are actually free; the next attempt binds them.
for entry in "${NODES[@]}"; do
    read -r name offset port root <<< "$entry"
    for i in $(seq 1 20); do
        (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null || break
        exec 3<&- 2>/dev/null || true
        sleep 1
        [ "$i" = 20 ] && echo "WARNING: port $port ($name) is still bound."
    done
done
echo "All nodes stopped."
"""

_DG_TEST_SH = """\
#!/bin/bash
# Data Grid reproducer: write entries, kill the owner node, read them back.
# Prints "ISSUE REPRODUCED" if data that should have survived did not.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/nodes.env"

CACHE="${CACHE:-reproducer}"
ENTRIES="${ENTRIES:-200}"

# DIGEST, not Basic: the properties realm holds only hashed credentials, so a
# Basic header is rejected with "ISPN080052: ... mechanism 'null' is not
# supported" -- a 403 that looks exactly like a missing key if you only count
# non-200 responses.
AUTH=(--digest -u "$DG_USER:$DG_PASS")

read -r n1 o1 P1 r1 <<< "${NODES[0]}"
read -r n2 o2 P2 r2 <<< "${NODES[1]:-${NODES[0]}}"

echo "=========================================================="
echo "  Data Grid Reproducer: $ISSUE_LABEL"
echo "=========================================================="
echo

inconclusive() {
    echo
    echo "=========================================================="
    echo "  INCONCLUSIVE: $1"
    echo "  No verdict is reported -- the reproducer could not run far"
    echo "  enough to tell a real failure from a broken setup."
    echo "=========================================================="
    exit 2
}

echo "Step 0: check the REST endpoint answers and is authenticated..."
probe="$(curl -s "${AUTH[@]}" -o /dev/null -w '%{http_code}' \\
         "http://localhost:$P1/rest/v2/caches" 2>/dev/null || true)"
if [ "$probe" != "200" ]; then
    echo "   GET /rest/v2/caches on $n1 returned HTTP $probe"
    inconclusive "cannot talk to $n1 (HTTP $probe). 401/403 means the endpoint user
  '$DG_USER' is missing or the auth mechanism is wrong; 000 means the node is down."
fi
echo "   OK (HTTP 200 as $DG_USER)"

echo "Step 1: create a distributed cache '$CACHE' (owners=2)..."
# Caches created over REST are permanent -- they survive in the server root and
# come back on the next boot, entries and all. Drop it first so each attempt
# starts from an empty cache instead of reading a previous run's data.
curl -s "${AUTH[@]}" -o /dev/null -X DELETE \\
     "http://localhost:$P1/rest/v2/caches/$CACHE" 2>/dev/null || true
sleep 1
body="$(curl -s "${AUTH[@]}" -w '\\n%{http_code}' \\
        -X POST -H 'Content-Type: application/json' \\
        -d '{"distributed-cache":{"mode":"SYNC","owners":2,"statistics":true}}' \\
        "http://localhost:$P1/rest/v2/caches/$CACHE")"
code="$(echo "$body" | tail -1)"
case "$code" in
    200|204) echo "   created" ;;
    *)       inconclusive "could not create the cache (HTTP $code): $(echo "$body" | head -1)" ;;
esac
sleep 2

echo "Step 2: write $ENTRIES entries through $n1..."
write_fail=0
for i in $(seq 1 "$ENTRIES"); do
    code="$(curl -s "${AUTH[@]}" -o /dev/null -w '%{http_code}' \\
            -X POST -H 'Content-Type: text/plain' \\
            -d "value-$i" "http://localhost:$P1/rest/v2/caches/$CACHE/key-$i")"
    case "$code" in 200|204) ;; *) write_fail=$((write_fail + 1)) ;; esac
done
[ "$write_fail" -gt 0 ] && \\
    inconclusive "$write_fail of $ENTRIES writes failed, so nothing can be concluded
  from the reads afterwards."
size_before="$(curl -s "${AUTH[@]}" "http://localhost:$P1/rest/v2/caches/$CACHE?action=size")"
echo "   cache size via $n1: $size_before"
case "$size_before" in
    ''|*[!0-9]*) inconclusive "cache size came back as '$size_before', not a number." ;;
esac

echo
echo "Step 3: kill $n2 abruptly and read every key back from $n1..."
# Match the server's own argv (-n <name> -s <root>), not the run-node.sh
# wrapper: killing the wrapper leaves the JVM orphaned and still serving,
# so the "node loss" never happens and the reads all succeed for the
# wrong reason.
pids="$(pgrep -f -- "-n $n2 -s .*$r2" || true)"
if [ -z "$pids" ]; then
    inconclusive "could not find the $n2 process to kill; the node-loss step
  never happened."
fi
echo "   >>> kill -9 $n2 (PIDs: $(echo $pids | tr '\\n' ' '))"
kill -9 $pids 2>/dev/null || true

# Wait for the port to actually stop answering, then for the survivors to
# install a new view. A fixed sleep either wastes time or reads mid-rebalance.
for i in $(seq 1 30); do
    curl -s -o /dev/null --max-time 2 "http://localhost:$P2/rest/v2/caches" 2>/dev/null || break
    sleep 1
done
echo -n "   waiting for the surviving nodes to install a new view..."
want=$(( ${#NODES[@]} - 1 ))
for i in $(seq 1 60); do
    size="$(curl -s "${AUTH[@]}" "http://localhost:$P1/rest/v2/cache-managers/default" \\
            | tr ',' '\\n' | grep -i cluster_size | grep -oE '[0-9]+' | head -1)"
    if [ "${size:-0}" = "$want" ]; then echo " cluster_size=$size"; break; fi
    [ "$i" = 60 ] && echo " still $size after 60s (continuing anyway)"
    sleep 1
done
sleep 5

lost=0; wrong=0; errors=0; first_error=""
for i in $(seq 1 "$ENTRIES"); do
    body="$(curl -s "${AUTH[@]}" -w '\\n%{http_code}' \\
            "http://localhost:$P1/rest/v2/caches/$CACHE/key-$i" 2>/dev/null)"
    code="$(echo "$body" | tail -1)"
    val="$(echo "$body" | head -1)"
    case "$code" in
        200) [ "$val" = "value-$i" ] || wrong=$((wrong + 1)) ;;
        404) lost=$((lost + 1)) ;;
        # Anything else is a broken reproducer, not lost data. 403 is the
        # classic: wrong auth scheme makes all 200 keys look "missing".
        *)   errors=$((errors + 1)); [ -z "$first_error" ] && first_error="HTTP $code: $val" ;;
    esac
done
size_after="$(curl -s "${AUTH[@]}" "http://localhost:$P1/rest/v2/caches/$CACHE?action=size")"

echo
echo "=========================================================="
echo "  entries written : $ENTRIES"
echo "  size before/after node loss : $size_before / $size_after"
echo "  lost (HTTP 404) after node loss : $lost"
echo "  wrong value after node loss     : $wrong"
echo "  read errors (not data loss)     : $errors"
if [ "$errors" -gt 0 ]; then
    echo "  first read error: $first_error"
    inconclusive "$errors of $ENTRIES reads failed with an error rather than 404.
  That is a broken reproducer, not lost data."
fi
if [ "$lost" -gt 0 ] || [ "$wrong" -gt 0 ]; then
    echo
    echo "  ISSUE REPRODUCED: data did not survive the loss of one owner,"
    echo "  which owners=2 is supposed to guarantee."
else
    echo
    echo "  Issue NOT reproduced this run: every entry survived."
    echo "  Re-run with --repeat, or raise ENTRIES / add nodes to widen the window."
fi
echo "=========================================================="
"""


_DG_RUN_REPRODUCER_SH = """\
#!/bin/bash
# One command: resolve install + JDK, start the cluster, run the test, repeat.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/nodes.env"

REPEAT=1
TERMINAL_FLAG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --repeat) REPEAT="${2:?--repeat needs a number}"; shift 2 ;;
        --no-terminals) TERMINAL_FLAG="--no-terminals"; shift ;;
        *) echo "usage: run-reproducer.sh [--repeat N] [--no-terminals]"; exit 1 ;;
    esac
done

hits=0
bad=0
for attempt in $(seq 1 "$REPEAT"); do
    echo
    echo "##############  ATTEMPT $attempt / $REPEAT  ##############"
    "$SCRIPT_DIR/stop-cluster.sh" >/dev/null 2>&1 || true
    sleep 2
    if ! "$SCRIPT_DIR/start-cluster.sh" $TERMINAL_FLAG; then
        echo "Cluster failed to start; aborting."
        exit 1
    fi
    "$SCRIPT_DIR/test.sh" 2>&1 | tee "$SCRIPT_DIR/attempt-$attempt.log"
    if grep -q "INCONCLUSIVE" "$SCRIPT_DIR/attempt-$attempt.log"; then
        bad=$((bad + 1))
    elif grep -q "ISSUE REPRODUCED" "$SCRIPT_DIR/attempt-$attempt.log"; then
        hits=$((hits + 1))
    fi
done

"$SCRIPT_DIR/stop-cluster.sh" >/dev/null 2>&1 || true

good=$((REPEAT - bad))
echo
echo "=========================================================="
if [ "$bad" -gt 0 ]; then
    echo "  $bad of $REPEAT attempt(s) were INCONCLUSIVE (see the reason above)."
    echo "  Those are not counted either way."
fi
echo "  Reproduced on $hits of $good usable attempt(s)"
if [ "$hits" = 0 ] && [ "$good" -gt 0 ]; then
    echo "  The issue did NOT reproduce on this build."
    echo "  That is itself a result: it suggests the bug is not present in"
    echo "  this Data Grid version. Match the customer's exact micro-version"
    echo "  before concluding, then advise an upgrade if the latest is clean."
fi
echo "=========================================================="
[ "$good" = 0 ] && exit 2
exit 0
"""

# --- JVM (heap / GC / metaspace / thread) ----------------------------------
# A JVM issue reproduces in one JVM. No server home, no cluster, no war -- what
# it needs is the customer's flags and the diagnostics that make the failure
# analysable after the fact.

_JVM_RUN_REPRODUCER_SH = """\
#!/bin/bash
# One command: resolve a JDK, compile the workload, run it under the customer's
# JVM flags with full diagnostics, and report whether the failure happened.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/jvm.env"

REPEAT=1
while [ $# -gt 0 ]; do
    case "$1" in
        --repeat) REPEAT="${2:?--repeat needs a number}"; shift 2 ;;
        --no-terminals) shift ;;   # accepted for symmetry with the other packages
        *) echo "usage: run-reproducer.sh [--repeat N]"; exit 1 ;;
    esac
done

if ! jdk_export="$("$SCRIPT_DIR/ensure-jdk.sh" --export)"; then
    echo "ABORTED: no usable JDK (see above)." >&2
    exit 1
fi
eval "$jdk_export"
export JAVA_HOME
echo "JAVA_HOME=$JAVA_HOME"
"$JAVA_HOME/bin/java" -version 2>&1 | sed 's/^/  /'

DUMPS="$SCRIPT_DIR/dumps"
mkdir -p "$DUMPS" "$SCRIPT_DIR/classes"

echo "Compiling workload..."
"$JAVA_HOME/bin/javac" -d "$SCRIPT_DIR/classes" "$SCRIPT_DIR/src/Workload.java" || exit 1

hits=0
for attempt in $(seq 1 "$REPEAT"); do
    echo
    echo "##############  ATTEMPT $attempt / $REPEAT  ##############"
    rm -f "$DUMPS"/*.hprof "$SCRIPT_DIR/gc.log" 2>/dev/null || true
    LOG="$SCRIPT_DIR/attempt-$attempt.log"

    # The diagnostics that make a JVM issue both reproducible AND analysable:
    #   HeapDumpOnOutOfMemoryError -> a .hprof to open in Eclipse MAT
    #   -Xlog:gc*                  -> pause times and allocation rate
    #   ExitOnOutOfMemoryError off -> we want the dump, then a clean report
    set -x
    "$JAVA_HOME/bin/java" $JVM_OPTS \\
        -XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath="$DUMPS" \\
        -Xlog:gc*:file="$SCRIPT_DIR/gc.log":time,uptime,level,tags \\
        -cp "$SCRIPT_DIR/classes" Workload $WORKLOAD_ARGS > "$LOG" 2>&1
    status=$?
    set +x

    tail -25 "$LOG"

    if grep -qE "OutOfMemoryError|StackOverflowError|ISSUE REPRODUCED" "$LOG" \\
       || ls "$DUMPS"/*.hprof >/dev/null 2>&1; then
        hits=$((hits + 1))
        echo "  ISSUE REPRODUCED (exit $status)"
        ls -la "$DUMPS"/*.hprof 2>/dev/null | sed 's/^/    heap dump: /'
    else
        echo "  Issue NOT reproduced this run (exit $status)"
    fi
done

echo
echo "=========================================================="
echo "  Reproduced on $hits of $REPEAT attempt(s)"
echo "  gc log     : $SCRIPT_DIR/gc.log"
echo "  heap dumps : $DUMPS"
echo "  Take a thread dump of a live run with ./capture.sh"
if [ "$hits" = 0 ]; then
    echo
    echo "  The issue did NOT reproduce with these flags. Check jvm.env against"
    echo "  the customer's real JAVA_OPTS -- heap size and GC choice are usually"
    echo "  what decides whether this fails."
fi
echo "=========================================================="
"""


_JVM_CAPTURE_SH = """\
#!/bin/bash
# Capture diagnostics from a running JVM: thread dumps on a timer, plus a heap
# histogram. Three dumps a few seconds apart is what distinguishes a real
# deadlock from a thread that merely looked busy once.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COUNT="${1:-3}"
GAP="${2:-5}"
OUT="$SCRIPT_DIR/dumps"
mkdir -p "$OUT"

if ! jdk_export="$("$SCRIPT_DIR/ensure-jdk.sh" --export)"; then exit 1; fi
eval "$jdk_export"

PID="${PID:-$("$JAVA_HOME/bin/jps" -l 2>/dev/null | grep Workload | awk '{print $1}' | head -1)}"
if [ -z "${PID:-}" ]; then
    echo "No Workload JVM running. Start one with ./run-reproducer.sh, or set PID=<pid>." >&2
    exit 1
fi
echo "Capturing from PID $PID"

for i in $(seq 1 "$COUNT"); do
    stamp="$(date +%Y%m%d-%H%M%S)"
    "$JAVA_HOME/bin/jstack" -l "$PID" > "$OUT/threaddump-$stamp.txt"
    echo "  $OUT/threaddump-$stamp.txt"
    [ "$i" -lt "$COUNT" ] && sleep "$GAP"
done

"$JAVA_HOME/bin/jcmd" "$PID" GC.class_histogram > "$OUT/histogram-$(date +%H%M%S).txt" 2>/dev/null \\
    && echo "  heap histogram written"
echo "Drop these back into the case bundle under dumps/."
"""

_JVM_WORKLOAD_JAVA = """\
import java.lang.management.ManagementFactory;
import java.lang.management.ThreadMXBean;
import java.util.*;
import java.util.concurrent.*;

/**
 * Drives the three JVM failures that show up in support cases. Which one runs
 * is chosen by argv[0] so the same reproducer covers all of them:
 *
 *   heap      retained allocation -> OutOfMemoryError: Java heap space
 *   metaspace classloader leak    -> OutOfMemoryError: Metaspace
 *   threads   lock-ordering bug   -> a real, jstack-visible deadlock
 *
 * Edit jvm.env, not this file, to match the customer's heap and collector.
 */
public class Workload {

    public static void main(String[] args) throws Exception {
        String mode = args.length > 0 ? args[0] : "heap";
        int iterations = args.length > 1 ? Integer.parseInt(args[1]) : 100000;
        System.out.println("mode=" + mode + " iterations=" + iterations
                + " maxHeap=" + (Runtime.getRuntime().maxMemory() >> 20) + "m");

        switch (mode) {
            case "metaspace": metaspace(iterations); break;
            case "threads":   threads();             break;
            default:          heap(iterations);      break;
        }
    }

    /** Retained allocation: the collector cannot help, so the heap fills. */
    private static void heap(int iterations) {
        List<byte[]> retained = new ArrayList<>();
        for (int i = 0; i < iterations; i++) {
            retained.add(new byte[64 * 1024]);
            if (i % 200 == 0) {
                long used = Runtime.getRuntime().totalMemory()
                        - Runtime.getRuntime().freeMemory();
                System.out.println("  allocated " + i + " blocks, heap used "
                        + (used >> 20) + "m");
            }
        }
        System.out.println("Completed without OOM -- raise iterations or lower -Xmx.");
    }

    /**
     * Metaspace leak: every iteration defines a new class in a new loader and
     * keeps the loader reachable. This is the shape of the real thing -- a
     * redeploy loop that leaks the application classloader.
     */
    private static void metaspace(int iterations) {
        List<ClassLoader> loaders = new ArrayList<>();
        // A minimal valid class file (class Tiny {}), redefined under a new
        // name each time so Metaspace grows instead of being shared.
        for (int i = 0; i < iterations; i++) {
            final int n = i;
            ClassLoader cl = new ClassLoader(Workload.class.getClassLoader()) {
                @Override
                protected Class<?> findClass(String name) throws ClassNotFoundException {
                    byte[] bytes = tinyClass("Tiny" + n);
                    return defineClass(name, bytes, 0, bytes.length);
                }
            };
            try {
                loaders.add(cl);
                Class.forName("Tiny" + n, true, cl);
            } catch (Throwable t) {
                if (t instanceof OutOfMemoryError) throw (OutOfMemoryError) t;
            }
            if (i % 500 == 0) System.out.println("  loaded " + i + " classes");
        }
        System.out.println("Completed without OOM -- lower -XX:MaxMetaspaceSize.");
    }

    /** Two threads taking two locks in opposite orders: a textbook deadlock. */
    private static void threads() throws Exception {
        final Object lockA = new Object();
        final Object lockB = new Object();
        CountDownLatch both = new CountDownLatch(2);

        Thread t1 = new Thread(() -> {
            synchronized (lockA) {
                both.countDown();
                await(both);
                synchronized (lockB) { System.out.println("t1 got both"); }
            }
        }, "reproducer-thread-1");

        Thread t2 = new Thread(() -> {
            synchronized (lockB) {
                both.countDown();
                await(both);
                synchronized (lockA) { System.out.println("t2 got both"); }
            }
        }, "reproducer-thread-2");

        t1.start();
        t2.start();
        Thread.sleep(3000);

        ThreadMXBean mx = ManagementFactory.getThreadMXBean();
        long[] deadlocked = mx.findDeadlockedThreads();
        if (deadlocked != null && deadlocked.length > 0) {
            System.out.println("ISSUE REPRODUCED: " + deadlocked.length
                    + " threads deadlocked");
            for (long id : deadlocked) {
                System.out.println("  " + mx.getThreadInfo(id, 8));
            }
            System.out.println("Take a thread dump now with ./capture.sh");
            Thread.sleep(60000);   // stay alive so jstack can see it
        } else {
            System.out.println("No deadlock detected this run.");
        }
        System.exit(0);
    }

    private static void await(CountDownLatch latch) {
        try { latch.await(2, TimeUnit.SECONDS); } catch (InterruptedException ignored) { }
    }

    /** Bytes of `class <name> {}` -- enough for defineClass to consume. */
    private static byte[] tinyClass(String name) {
        byte[] n = name.getBytes();
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
        java.io.DataOutputStream d = new java.io.DataOutputStream(out);
        try {
            d.writeInt(0xCAFEBABE);
            d.writeShort(0); d.writeShort(50);      // minor, major (Java 6)
            d.writeShort(5);                        // constant pool count
            d.writeByte(7); d.writeShort(2);        // #1 Class -> #2
            d.writeByte(1); d.writeShort(n.length); d.write(n);   // #2 Utf8 name
            d.writeByte(7); d.writeShort(4);        // #3 Class -> #4
            d.writeByte(1); d.writeShort(16); d.writeBytes("java/lang/Object");
            d.writeShort(0x0021);                   // public super
            d.writeShort(1); d.writeShort(3);       // this, super
            d.writeShort(0); d.writeShort(0); d.writeShort(0);  // ifaces, fields, methods
            d.writeShort(0);                        // attributes
        } catch (java.io.IOException e) {
            throw new RuntimeException(e);
        }
        return out.toByteArray();
    }
}
"""

_RUN_NODE_SH = """\
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
    if ! eap_export="$("$SCRIPT_DIR/ensure-server-home.sh" --export)"; then
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

"$EAP_HOME/bin/standalone.sh" \\
    -c "$SERVER_CONFIG" \\
    -Djboss.server.base.dir="$BASE" \\
    -Djboss.node.name="$NAME" \\
    -Djboss.socket.binding.port-offset="$OFFSET" \\
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
"""


_RUN_REPRODUCER_SH = """\
#!/bin/bash
# One command to go from "nothing running" to a verdict:
#
#     ./run-reproducer.sh [--repeat N] [--no-terminals]
#
#   1. provisions a compatible JDK (downloads one if the system has none)
#   2. builds the application
#   3. starts every node, each in its own terminal window
#   4. starts the sticky load balancer (if this reproducer bundles one)
#   5. runs the test and reports whether the issue reproduced
#
# The failure being reproduced is intermittent, so --repeat re-runs the whole
# cycle; the test kills nodes, hence each attempt needs a fresh cluster.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/nodes.env"

REPEAT=1
TERMINAL_ARGS=()
while [ $# -gt 0 ]; do
    case "$1" in
        --repeat)       REPEAT="${2:?--repeat needs a number}"; shift 2 ;;
        --no-terminals) TERMINAL_ARGS+=(--no-terminals); shift ;;
        -h|--help)      sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

# `eval "$(cmd)"` hides cmd's exit status from set -e, so capture and check first.
resolve() {  # resolve <script> -- aborts if the helper rejects the environment
    local out
    if ! out="$("$SCRIPT_DIR/$1" --export)"; then
        echo "ABORTED: $1 refused to run this reproducer here (see above)." >&2
        exit 1
    fi
    eval "$out"
}

resolve ensure-server-home.sh
resolve ensure-jdk.sh
export EAP_HOME
export JAVA_HOME

if [ ! -f "$SCRIPT_DIR/app/target/reproducer.war" ]; then
    bash "$SCRIPT_DIR/build.sh"
fi

hits=0
for attempt in $(seq 1 "$REPEAT"); do
    echo ""
    echo "##############  ATTEMPT $attempt / $REPEAT  ##############"
    "$SCRIPT_DIR/stop-cluster.sh" >/dev/null 2>&1 || true
    "$SCRIPT_DIR/start-cluster.sh" "${TERMINAL_ARGS[@]+"${TERMINAL_ARGS[@]}"}" || {
        echo "ERROR: cluster did not start; see the per-node terminals or *.log"; exit 1
    }
    if [ -x "$SCRIPT_DIR/lb/start-lb.sh" ]; then
        # Restart it: a balancer left over from the previous attempt still holds
        # port 8000, and its stale worker state would skew the next run.
        [ -x "$SCRIPT_DIR/lb/stop-lb.sh" ] && "$SCRIPT_DIR/lb/stop-lb.sh" >/dev/null 2>&1
        "$SCRIPT_DIR/lb/start-lb.sh"
    fi

    out="$SCRIPT_DIR/attempt-$attempt.log"
    "$SCRIPT_DIR/test.sh" 2>&1 | tee "$out"
    if grep -q "ISSUE REPRODUCED" "$out"; then
        hits=$((hits + 1))
        echo ">>> reproduced on attempt $attempt (log: $out)"
    fi
done

echo ""
echo "=========================================================="
echo "  Reproduced on $hits of $REPEAT attempt(s)"
if [ "$hits" -eq 0 ]; then
    echo "  The issue did NOT reproduce on this build."
    echo "  That is itself a result: it suggests the bug is not present in"
    echo "  this EAP version. Match the customer's exact micro-version before"
    echo "  concluding, then advise an upgrade if the latest CP is clean."
fi
echo "=========================================================="
"""


_START_CLUSTER_SH = """\
#!/bin/bash
# Start the __NUM_NODES__-node EAP cluster for reproducing: __ISSUE__
#
# Each node comes up in ITS OWN TERMINAL WINDOW, so you can watch the servers
# boot the way you would in a real lab. Pass --no-terminals to keep them in the
# background writing <node>.log instead (use that over SSH or in CI).
#
# A missing or incompatible JDK is not your problem to solve: ensure-jdk.sh
# picks the right one for this EAP release and downloads it if the machine
# hasn't got one.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/nodes.env"

USE_TERMINALS=1
[ "${1:-}" = "--no-terminals" ] && USE_TERMINALS=0

# `eval "$(cmd)"` hides cmd's exit status from set -e, so capture and check first.
resolve() {  # resolve <script> -- aborts if the helper rejects the environment
    local out
    if ! out="$("$SCRIPT_DIR/$1" --export)"; then
        echo "ABORTED: $1 refused to run this reproducer here (see above)." >&2
        exit 1
    fi
    eval "$out"
}

# The case file decides which server this runs against, not the ambient shell.
resolve ensure-server-home.sh
resolve ensure-jdk.sh
export JAVA_HOME EAP_HOME

if [ ! -f "$SCRIPT_DIR/app/target/reproducer.war" ]; then
    echo "Application not built. Running build.sh first..."
    bash "$SCRIPT_DIR/build.sh"
fi

# --- where do the node windows come from? ----------------------------------
detect_terminal() {
    local t
    for t in ptyxis gnome-terminal konsole xfce4-terminal kitty alacritty xterm tmux; do
        command -v "$t" >/dev/null 2>&1 && { echo "$t"; return; }
    done
    echo none
}
TERM_EMU="$(detect_terminal)"
if [ "$USE_TERMINALS" = 1 ]; then
    if [ "$TERM_EMU" = none ]; then
        echo "No terminal emulator found - starting nodes in the background instead."
        USE_TERMINALS=0
    elif [ "$TERM_EMU" != tmux ] && [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
        echo "No graphical display - starting nodes in the background instead."
        USE_TERMINALS=0
    fi
fi

launch_node() {  # launch_node <name> <offset> <base-dir-name>
    local name=$1 offset=$2 basedir=$3 cmd
    # gnome-terminal and ptyxis run as D-Bus services: a window they open does
    # NOT inherit this shell's environment, so hand the settings over on the
    # command line rather than exporting and hoping.
    cmd="EAP_HOME=$(printf '%q' "$EAP_HOME") JAVA_HOME=$(printf '%q' "$JAVA_HOME")"
    cmd="$cmd $(printf '%q' "$SCRIPT_DIR/run-node.sh") $name $offset $basedir"

    if [ "$USE_TERMINALS" = 0 ]; then
        nohup bash -c "$cmd" >/dev/null 2>&1 &
        echo "  $name -> background (log: $name.log)"
        return
    fi
    case "$TERM_EMU" in
        ptyxis)         setsid ptyxis --new-window --title "$name" -- bash -c "$cmd" >/dev/null 2>&1 & ;;
        gnome-terminal) setsid gnome-terminal --title="$name" -- bash -c "$cmd" >/dev/null 2>&1 & ;;
        konsole)        setsid konsole -p tabtitle="$name" -e bash -c "$cmd" >/dev/null 2>&1 & ;;
        xfce4-terminal) setsid xfce4-terminal --title="$name" -x bash -c "$cmd" >/dev/null 2>&1 & ;;
        kitty)          setsid kitty --title "$name" bash -c "$cmd" >/dev/null 2>&1 & ;;
        alacritty)      setsid alacritty --title "$name" -e bash -c "$cmd" >/dev/null 2>&1 & ;;
        xterm)          setsid xterm -T "$name" -e bash -c "$cmd" >/dev/null 2>&1 & ;;
        tmux)           tmux new-session -d -s "repro-$name" "bash -c $(printf '%q' "$cmd")" ;;
    esac
    echo "  $name -> $TERM_EMU window (also logged to $name.log)"
}

wait_for_node() {
    local name=$1 mgmt_port=$2 timeout=180 elapsed=0
    echo "Waiting for $name (mgmt port $mgmt_port)..."
    while [ $elapsed -lt $timeout ]; do
        if "$EAP_HOME/bin/jboss-cli.sh" --connect --controller=localhost:$mgmt_port \\
            --command=":read-attribute(name=server-state)" 2>/dev/null | grep -q "running"; then
            echo "$name is RUNNING"
            return 0
        fi
        sleep 2; elapsed=$((elapsed + 2))
    done
    echo "ERROR: $name did not start within ${timeout}s - check its window or $name.log"
    return 1
}

apply_cli() {
    local mgmt_port=$1 f
    for f in configure-eap.cli instance-id.cli local/instance-id.cli; do
        if [ -f "$SCRIPT_DIR/$f" ]; then
            "$EAP_HOME/bin/jboss-cli.sh" --connect --controller=localhost:$mgmt_port \\
                --file="$SCRIPT_DIR/$f" >/dev/null 2>&1 || true
        fi
    done
}

echo "Launching __NUM_NODES__ nodes..."
for entry in "${NODES[@]}"; do
    read -r name offset http mgmt basedir <<< "$entry"
    launch_node "$name" "$offset" "$basedir"
done

for entry in "${NODES[@]}"; do
    read -r name offset http mgmt basedir <<< "$entry"
    wait_for_node "$name" "$mgmt"
done

# instance-id gives JSESSIONID its node route suffix (sticky LB); it triggers a
# :reload, so wait for every node to come back afterwards.
for entry in "${NODES[@]}"; do
    read -r name offset http mgmt basedir <<< "$entry"
    echo "Applying CLI configuration to $name..."
    apply_cli "$mgmt"
done
for entry in "${NODES[@]}"; do
    read -r name offset http mgmt basedir <<< "$entry"
    sleep 2
    wait_for_node "$name" "$mgmt"
done

__CLUSTER_CHECK__

echo ""
echo "=========================================="
echo "  __NUM_NODES__-node cluster started"
echo "=========================================="
for entry in "${NODES[@]}"; do
    read -r name offset http mgmt basedir <<< "$entry"
    echo "  $name: http://localhost:$http/reproducer/session"
done
echo ""
__NEXT_STEPS__
echo "Run ./stop-cluster.sh to stop all nodes"
"""


_STOP_CLUSTER_SH = """\
#!/bin/bash
# Stop every node: graceful :shutdown first, then hard-kill any survivor.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/nodes.env"
if [ -z "${EAP_HOME:-}" ]; then
    eval "$("$SCRIPT_DIR/ensure-server-home.sh" --export)" 2>/dev/null || EAP_HOME=""
fi

for entry in "${NODES[@]}"; do
    read -r name offset http mgmt basedir <<< "$entry"
    if [ -n "$EAP_HOME" ] && [ -f "$EAP_HOME/bin/jboss-cli.sh" ]; then
        "$EAP_HOME/bin/jboss-cli.sh" --connect --controller=localhost:$mgmt \\
            --command=":shutdown" >/dev/null 2>&1 && echo "$name: shutdown requested"
    fi
done

sleep 3

# Match on jboss.node.name: the terminal window owns the standalone.sh PID, so
# a recorded PID file is not a reliable handle on the actual JVM.
for entry in "${NODES[@]}"; do
    read -r name offset http mgmt basedir <<< "$entry"
    pids="$(pgrep -f "jboss.node.name=$name" || true)"
    if [ -n "$pids" ]; then
        echo "$name: hard-killing $pids"
        kill -9 $pids 2>/dev/null || true
    fi
    rm -f "$SCRIPT_DIR/$name.pid"
done

echo "All nodes stopped."
"""


class ReproducerGenerator:
    """Generates complete reproducer packages."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        provider: str | None = None,
    ) -> None:
        self._llm = LLMClient(
            api_key=api_key,
            model=model,
            base_url=base_url,
            provider=provider,
        )

    def generate(self, config: ReproducerConfig) -> str:
        if config.issue_analysis is None:
            raise ValueError("ReproducerConfig must include an IssueAnalysis")

        analysis = config.issue_analysis
        output_dir = Path(config.output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        namespace = self._determine_namespace(analysis)
        server_config = self._determine_server_config(analysis)

        if self._llm.available:
            self._generate_with_llm(config, output_dir, namespace, server_config)
        else:
            self._generate_full_fallback(config, output_dir, namespace, server_config)

        return str(output_dir)

    # ------------------------------------------------------------------
    # LLM-powered generation
    # ------------------------------------------------------------------

    def _generate_with_llm(
        self, config: ReproducerConfig, output_dir: Path,
        namespace: str, server_config: str,
    ) -> None:
        analysis = config.issue_analysis
        prompt = self._build_full_prompt(analysis, namespace, server_config, config)

        content = self._llm.generate(prompt, _GENERATOR_SYSTEM_PROMPT, max_tokens=16000)
        files = self._parse_file_map(content)

        if files:
            self._write_files(output_dir, files)
        else:
            self._generate_full_fallback(config, output_dir, namespace, server_config)

    def _build_full_prompt(
        self, analysis: IssueAnalysis, namespace: str,
        server_config: str, config: ReproducerConfig,
    ) -> str:
        return (
            f"Generate a COMPLETE reproducer package for this issue:\n\n"
            f"Product: {analysis.product} {analysis.version}\n"
            f"Namespace: {namespace}.*\n"
            f"Server Config: {server_config}\n"
            f"Subsystem: {analysis.subsystem}\n"
            f"Nodes: {analysis.num_nodes}\n"
            f"Deployment: {analysis.deployment_type}\n"
            f"App Type: {analysis.application_type}\n"
            f"Trigger: {analysis.trigger}\n"
            f"Error: {analysis.error_signature}\n"
            f"Root Cause: {analysis.possible_root_cause}\n"
            f"Categories: {', '.join(analysis.categories)}\n"
            f"Strategy: {analysis.reproducer_strategy}\n"
            f"Topology: {analysis.topology_description}\n"
            f"Expected: {analysis.expected_behavior}\n"
            f"Actual: {analysis.actual_behavior}\n\n"
            f"Generate these files:\n"
            f"1. app/pom.xml - Maven POM with {namespace}.* dependencies\n"
            f"2. app/src/main/java/com/redhat/reproducer/*.java - App source\n"
            f"3. app/src/main/webapp/WEB-INF/web.xml\n"
            f"4. build.sh - Build script\n"
            f"5. start-cluster.sh - Start ALL {analysis.num_nodes} nodes with {server_config}\n"
            f"6. test.sh - Script that triggers the exact issue ({analysis.trigger})\n"
            f"7. stop-cluster.sh - Stop all nodes\n"
            f"8. README.md - Full reproduction steps\n"
        )

    # ------------------------------------------------------------------
    # Smart fallback — generates issue-specific reproducers without LLM
    # ------------------------------------------------------------------

    def _generate_full_fallback(
        self, config: ReproducerConfig, output_dir: Path,
        namespace: str, server_config: str,
    ) -> None:
        analysis = config.issue_analysis
        kind = self._product_kind(analysis)
        if kind == "datagrid":
            self._write_files(output_dir, self._gen_datagrid_package(config, server_config))
            return
        if kind == "jvm":
            self._write_files(output_dir, self._gen_jvm_package(config, namespace))
            return

        files: dict[str, str] = {}

        # 1. Application
        files.update(self._gen_app(analysis, namespace))

        # 2. Build script
        files["build.sh"] = self._gen_build_script()

        # 3. Topology + the scripts that consume it. Every node runs in its own
        #    terminal window, and a compatible JDK is provisioned automatically
        #    (downloaded when the machine has none) -- an unbootable JVM is the
        #    single most common reason a reproducer "does not work".
        files["nodes.env"] = self._gen_nodes_env(analysis, server_config)
        files["ensure-server-home.sh"] = _ENSURE_SERVER_HOME_SH
        files["ensure-jdk.sh"] = _ENSURE_JDK_SH
        files["run-node.sh"] = _RUN_NODE_SH
        files["start-cluster.sh"] = self._gen_start_script(analysis, server_config)
        files["run-reproducer.sh"] = _RUN_REPRODUCER_SH

        # 4. Stop cluster script
        files["stop-cluster.sh"] = self._gen_stop_script(analysis)

        # 5. Test script (issue-specific)
        files["test.sh"] = self._gen_test_script(analysis)

        # 6. CLI configuration script
        files["configure-eap.cli"] = self._gen_cli_script(analysis, server_config)

        # 7. Self-contained clustering: sticky load balancer + instance-id CLI.
        #    Without an LB in front, a "session lost on failover" issue cannot be
        #    reproduced locally at all -- so we bundle one instead of listing it
        #    as an external prerequisite the user has to build themselves.
        if self._is_cluster_session(analysis):
            nodes = self._effective_nodes(analysis)
            files["lb/httpd.conf"] = self._gen_lb_config(nodes)
            files.update(self._gen_lb_scripts())
            files["instance-id.cli"] = self._gen_instance_id_cli()

        # 8. The customer's own configuration, if the case bundle supplied it.
        if config.customer_configs:
            files.update(self._gen_customer_configs(config))

        # 9. README
        files["README.md"] = self._gen_readme(analysis, server_config, config)

        # 10. OpenShift files if applicable
        if config.openshift_reproducer and self._needs_openshift(analysis):
            files.update(self._gen_openshift(analysis))

        self._write_files(output_dir, files)

    def _gen_datagrid_package(
        self, config: ReproducerConfig, server_config: str,
    ) -> dict[str, str]:
        """A Data Grid reproducer: no war, no load balancer, no Maven build.

        The workload is REST traffic against the server's own endpoint, so the
        whole thing is shell -- which also means it runs against any Data Grid
        install without compiling anything.
        """
        analysis = config.issue_analysis
        files = {
            "nodes.env": self._gen_nodes_env(analysis, server_config),
            "ensure-server-home.sh": _ENSURE_SERVER_HOME_SH,
            "ensure-jdk.sh": _ENSURE_JDK_SH,
            "run-node.sh": _DG_RUN_NODE_SH,
            "start-cluster.sh": _DG_START_CLUSTER_SH,
            "stop-cluster.sh": _DG_STOP_CLUSTER_SH,
            "test.sh": _DG_TEST_SH,
            "run-reproducer.sh": _DG_RUN_REPRODUCER_SH,
        }
        if config.customer_configs:
            files.update(self._gen_customer_configs(config))
        files["README.md"] = self._gen_readme(analysis, server_config, config)
        return files

    def _gen_jvm_package(
        self, config: ReproducerConfig, namespace: str,
    ) -> dict[str, str]:
        """A JVM reproducer: one JVM, the customer's flags, and full diagnostics.

        No cluster and no server home -- a heap/GC/metaspace issue reproduces in
        a single JVM, and dragging a whole EAP cluster into it only adds noise.
        What matters is that the run produces the artifacts an engineer needs to
        analyse it: a heap dump on OOM, a GC log, and thread dumps on a timer.
        """
        analysis = config.issue_analysis
        files = {
            "ensure-jdk.sh": _ENSURE_JDK_SH,
            "jvm.env": self._gen_jvm_env(analysis),
            "run-reproducer.sh": _JVM_RUN_REPRODUCER_SH,
            "capture.sh": _JVM_CAPTURE_SH,
        }
        files.update(self._gen_jvm_app(analysis))
        if config.customer_configs:
            files.update(self._gen_customer_configs(config))
        files["README.md"] = self._gen_readme(analysis, "n/a", config)
        return files

    def _gen_jvm_env(self, analysis: IssueAnalysis) -> str:
        """JVM flags for the run, seeded from the case and meant to be edited.

        Whether a heap issue reproduces is decided almost entirely by -Xmx and
        the collector, so these are pulled out into one file rather than buried
        in a script.
        """
        customer_opts = (analysis.jdk or "").strip()
        haystack = " ".join(
            [c.lower() for c in analysis.categories]
            + [(analysis.subsystem or "").lower(), (analysis.error_signature or "").lower(),
               (analysis.possible_root_cause or "").lower()]
        )
        if "metaspace" in haystack or "classload" in haystack or "permgen" in haystack:
            mode, extra = "metaspace", "-XX:MaxMetaspaceSize=64m"
        elif "deadlock" in haystack or "thread" in haystack:
            mode, extra = "threads", "-Xss256k"
        else:
            mode, extra = "heap", ""

        # The JDK major the customer runs on. GC behaviour and the default
        # collector differ enough between 8, 11, 17 and 21 that a heap issue
        # can be version-specific, so this is pinned rather than "whatever
        # java is on PATH".
        m = re.search(r"(?:1\.)?(\d{1,2})", customer_opts)
        required = m.group(1) if m else ""
        return (
            "# JVM flags for this reproducer. EDIT THESE to match the customer's\n"
            "# real JAVA_OPTS -- heap size and collector choice are usually what\n"
            "# decide whether the failure happens at all.\n"
            "#\n"
            "# Read by ensure-jdk.sh as well as run-reproducer.sh.\n"
            'TARGET_KIND="jvm"\n'
            "\n"
            f"# Case reports JDK: {customer_opts or 'not stated'}\n"
            + (f'REQUIRED_JDK="{required}"\n' if required else
               '# REQUIRED_JDK=""   # set this to the customer\'s JDK major\n')
            + "\n"
            f'JVM_OPTS="-Xms128m -Xmx128m {extra}"\n'
            "\n"
            "# Passed to the workload: <mode> <iterations>\n"
            f'WORKLOAD_ARGS="{mode} 100000"\n'
        )

    def _gen_jvm_app(self, analysis: IssueAnalysis) -> dict[str, str]:
        """A single-file workload that can drive the three common JVM failures."""
        return {"src/Workload.java": _JVM_WORKLOAD_JAVA}

    def _gen_customer_configs(self, config: ReproducerConfig) -> dict[str, str]:
        """Ship the customer's configuration next to the reproducer.

        Not applied automatically: their standalone-ha.xml carries their
        datasources, their realms and their bind addresses, and dropping it
        onto a different machine usually just fails to boot. What it is good
        for is diffing -- if the reproducer passes on stock config and fails on
        theirs, the difference between the two files IS the bug.
        """
        files: dict[str, str] = {}
        for name, content in config.customer_configs.items():
            files[f"customer-configs/{name}"] = content

        names = sorted(config.customer_configs)
        listing = "\n".join(f"#   customer-configs/{n}" for n in names)
        files["diff-customer-config.sh"] = f'''#!/bin/bash
# Compare the customer's configuration against the stock files this reproducer
# runs on. The differences are the candidate causes.
#
# Supplied by the case bundle:
{listing}
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/nodes.env"

if ! eap_export="$("$SCRIPT_DIR/ensure-server-home.sh" --export)"; then
    echo "ABORTED: wrong or missing EAP installation (see above)." >&2
    exit 1
fi
eval "$eap_export"

shopt -s nullglob
for theirs in "$SCRIPT_DIR"/customer-configs/*; do
    name="$(basename "$theirs")"
    stock="$EAP_HOME/standalone/configuration/$name"
    [ -f "$stock" ] || stock="$EAP_HOME/domain/configuration/$name"
    if [ ! -f "$stock" ]; then
        echo "== $name: no stock counterpart in $EAP_HOME -- review by hand"
        continue
    fi
    echo "== $name (stock <-> customer)"
    diff -u "$stock" "$theirs" || true
    echo
done

echo "To run against the customer's config instead of stock:"
echo "  cp customer-configs/$SERVER_CONFIG \\"
echo "     \"$EAP_HOME/standalone-node1/configuration/$SERVER_CONFIG\""
echo "  # repeat per node, then fix bind addresses/datasources for this host."
'''
        return files

    # --- Application generation ---

    def _gen_app(self, analysis: IssueAnalysis, namespace: str) -> dict[str, str]:
        is_jakarta = namespace == "jakarta"
        java_ver = "17" if is_jakarta else "11"
        ee_version = "10.0.0" if is_jakarta else "8.0.1"
        ee_group = "jakarta.platform" if is_jakarta else "javax"
        ee_artifact = "jakarta.jakartaee-api" if is_jakarta else "javaee-api"
        web_schema = (
            'xmlns="https://jakarta.ee/xml/ns/jakartaee" version="6.0"'
            if is_jakarta else
            'xmlns="http://xmlns.jcp.org/xml/ns/javaee" version="4.0"'
        )

        pom = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<project xmlns="http://maven.apache.org/POM/4.0.0"\n'
            '         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
            '         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 '
            'http://maven.apache.org/xsd/maven-4.0.0.xsd">\n'
            '    <modelVersion>4.0.0</modelVersion>\n'
            '    <groupId>com.redhat.reproducer</groupId>\n'
            '    <artifactId>issue-reproducer</artifactId>\n'
            '    <version>1.0-SNAPSHOT</version>\n'
            '    <packaging>war</packaging>\n'
            '    <properties>\n'
            f'        <maven.compiler.source>{java_ver}</maven.compiler.source>\n'
            f'        <maven.compiler.target>{java_ver}</maven.compiler.target>\n'
            '        <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>\n'
            '        <failOnMissingWebXml>false</failOnMissingWebXml>\n'
            '    </properties>\n'
            '    <dependencies>\n'
            '        <dependency>\n'
            f'            <groupId>{ee_group}</groupId>\n'
            f'            <artifactId>{ee_artifact}</artifactId>\n'
            f'            <version>{ee_version}</version>\n'
            '            <scope>provided</scope>\n'
            '        </dependency>\n'
            '    </dependencies>\n'
            '    <build>\n'
            '        <finalName>reproducer</finalName>\n'
            '        <plugins>\n'
            '            <plugin>\n'
            '                <groupId>org.apache.maven.plugins</groupId>\n'
            '                <artifactId>maven-war-plugin</artifactId>\n'
            '                <version>3.3.2</version>\n'
            '            </plugin>\n'
            '            <plugin>\n'
            '                <groupId>org.apache.maven.plugins</groupId>\n'
            '                <artifactId>maven-compiler-plugin</artifactId>\n'
            '                <version>3.11.0</version>\n'
            '            </plugin>\n'
            '        </plugins>\n'
            '    </build>\n'
            '</project>\n'
        )

        is_distributable = any(c in analysis.categories for c in ["clustering", "session", "cache"])

        web_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<web-app {web_schema}>\n'
            '    <display-name>Issue Reproducer</display-name>\n'
        )
        if is_distributable:
            web_xml += '    <distributable/>\n'
        web_xml += '</web-app>\n'

        servlet = self._gen_servlet(analysis, namespace)

        files = {
            "app/pom.xml": pom,
            "app/src/main/webapp/WEB-INF/web.xml": web_xml,
        }
        files.update(servlet)
        return files

    def _gen_servlet(self, analysis: IssueAnalysis, ns: str) -> dict[str, str]:
        """Generate issue-specific servlet code."""
        cats = analysis.categories
        subsys = analysis.subsystem

        if "session" in cats or "clustering" in cats or subsys == "infinispan":
            return self._gen_session_servlet(analysis, ns)
        if "classloading" in cats:
            return self._gen_classloading_servlet(analysis, ns)
        return self._gen_basic_servlet(analysis, ns)

    def _gen_session_servlet(self, analysis: IssueAnalysis, ns: str) -> dict[str, str]:
        code = (
            f'package com.redhat.reproducer;\n\n'
            f'import java.io.IOException;\n'
            f'import java.io.PrintWriter;\n'
            f'import java.io.Serializable;\n'
            f'import java.util.logging.Logger;\n'
            f'import {ns}.servlet.ServletException;\n'
            f'import {ns}.servlet.annotation.WebServlet;\n'
            f'import {ns}.servlet.http.HttpServlet;\n'
            f'import {ns}.servlet.http.HttpServletRequest;\n'
            f'import {ns}.servlet.http.HttpServletResponse;\n'
            f'import {ns}.servlet.http.HttpSession;\n\n'
            f'@WebServlet("/session")\n'
            f'public class SessionReproducerServlet extends HttpServlet {{\n\n'
            f'    private static final Logger LOG = Logger.getLogger(SessionReproducerServlet.class.getName());\n\n'
            f'    @Override\n'
            f'    protected void doGet(HttpServletRequest req, HttpServletResponse resp)\n'
            f'            throws ServletException, IOException {{\n'
            f'        String nodeName = System.getProperty("jboss.node.name", "unknown");\n'
            f'        HttpSession session = req.getSession(true);\n\n'
            f'        // Handle delay parameter for concurrency testing\n'
            f'        String delay = req.getParameter("delay");\n'
            f'        if (delay != null) {{\n'
            f'            try {{ Thread.sleep(Long.parseLong(delay)); }}\n'
            f'            catch (InterruptedException e) {{ Thread.currentThread().interrupt(); }}\n'
            f'        }}\n\n'
            f'        // Increment session counter\n'
            f'        Integer counter = (Integer) session.getAttribute("counter");\n'
            f'        counter = (counter == null) ? 1 : counter + 1;\n'
            f'        session.setAttribute("counter", counter);\n\n'
            f'        // Store cart data to simulate customer scenario\n'
            f'        String item = req.getParameter("item");\n'
            f'        if (item != null) {{\n'
            f'            String cart = (String) session.getAttribute("cart");\n'
            f'            cart = (cart == null) ? item : cart + "," + item;\n'
            f'            session.setAttribute("cart", cart);\n'
            f'        }}\n\n'
            f'        LOG.info("Node=" + nodeName + " SessionID=" + session.getId()\n'
            f'                 + " Counter=" + counter + " New=" + session.isNew());\n\n'
            f'        resp.setContentType("text/plain");\n'
            f'        PrintWriter out = resp.getWriter();\n'
            f'        out.println("node=" + nodeName);\n'
            f'        out.println("sessionId=" + session.getId());\n'
            f'        out.println("counter=" + counter);\n'
            f'        out.println("isNew=" + session.isNew());\n'
            f'        out.println("cart=" + session.getAttribute("cart"));\n'
            f'    }}\n\n'
            f'    @Override\n'
            f'    protected void doDelete(HttpServletRequest req, HttpServletResponse resp)\n'
            f'            throws ServletException, IOException {{\n'
            f'        HttpSession session = req.getSession(false);\n'
            f'        if (session != null) {{\n'
            f'            LOG.info("Invalidating session: " + session.getId());\n'
            f'            session.invalidate();\n'
            f'        }}\n'
            f'        resp.getWriter().println("session invalidated");\n'
            f'    }}\n'
            f'}}\n'
        )
        return {"app/src/main/java/com/redhat/reproducer/SessionReproducerServlet.java": code}

    def _gen_classloading_servlet(self, analysis: IssueAnalysis, ns: str) -> dict[str, str]:
        code = (
            f'package com.redhat.reproducer;\n\n'
            f'import java.io.IOException;\n'
            f'import java.io.PrintWriter;\n'
            f'import java.util.logging.Logger;\n'
            f'import {ns}.servlet.ServletException;\n'
            f'import {ns}.servlet.annotation.WebServlet;\n'
            f'import {ns}.servlet.http.HttpServlet;\n'
            f'import {ns}.servlet.http.HttpServletRequest;\n'
            f'import {ns}.servlet.http.HttpServletResponse;\n\n'
            f'@WebServlet("/test")\n'
            f'public class ClassloadingReproducerServlet extends HttpServlet {{\n\n'
            f'    private static final Logger LOG = Logger.getLogger(ClassloadingReproducerServlet.class.getName());\n\n'
            f'    @Override\n'
            f'    protected void doGet(HttpServletRequest req, HttpServletResponse resp)\n'
            f'            throws ServletException, IOException {{\n'
            f'        resp.setContentType("text/plain");\n'
            f'        PrintWriter out = resp.getWriter();\n'
            f'        String className = req.getParameter("class");\n'
            f'        if (className == null) className = "javax.servlet.http.HttpServlet";\n\n'
            f'        try {{\n'
            f'            Class<?> clazz = Class.forName(className);\n'
            f'            out.println("FOUND: " + clazz.getName());\n'
            f'            out.println("ClassLoader: " + clazz.getClassLoader());\n'
            f'        }} catch (ClassNotFoundException e) {{\n'
            f'            out.println("NOT FOUND: " + className);\n'
            f'            out.println("Error: " + e.getMessage());\n'
            f'            LOG.severe("ClassNotFoundException: " + className);\n'
            f'        }}\n'
            f'    }}\n'
            f'}}\n'
        )
        return {"app/src/main/java/com/redhat/reproducer/ClassloadingReproducerServlet.java": code}

    def _gen_basic_servlet(self, analysis: IssueAnalysis, ns: str) -> dict[str, str]:
        code = (
            f'package com.redhat.reproducer;\n\n'
            f'import java.io.IOException;\n'
            f'import java.io.PrintWriter;\n'
            f'import java.util.logging.Logger;\n'
            f'import {ns}.servlet.ServletException;\n'
            f'import {ns}.servlet.annotation.WebServlet;\n'
            f'import {ns}.servlet.http.HttpServlet;\n'
            f'import {ns}.servlet.http.HttpServletRequest;\n'
            f'import {ns}.servlet.http.HttpServletResponse;\n\n'
            f'@WebServlet("/test")\n'
            f'public class ReproducerServlet extends HttpServlet {{\n\n'
            f'    private static final Logger LOG = Logger.getLogger(ReproducerServlet.class.getName());\n\n'
            f'    @Override\n'
            f'    protected void doGet(HttpServletRequest req, HttpServletResponse resp)\n'
            f'            throws ServletException, IOException {{\n'
            f'        String nodeName = System.getProperty("jboss.node.name", "unknown");\n'
            f'        resp.setContentType("text/plain");\n'
            f'        PrintWriter out = resp.getWriter();\n'
            f'        out.println("node=" + nodeName);\n'
            f'        out.println("status=ok");\n'
            f'        out.println("thread=" + Thread.currentThread().getName());\n'
            f'    }}\n'
            f'}}\n'
        )
        return {"app/src/main/java/com/redhat/reproducer/ReproducerServlet.java": code}

    # --- Script generation ---

    def _gen_build_script(self) -> str:
        return (
            '#!/bin/bash\n'
            '# Build the reproducer application\n'
            'set -euo pipefail\n'
            'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"\n\n'
            'echo "Building reproducer application..."\n'
            'cd "$SCRIPT_DIR/app"\n'
            'mvn clean package -DskipTests\n'
            'echo "Build complete: app/target/reproducer.war"\n'
        )

    def _product_kind(self, analysis: IssueAnalysis) -> str:
        """Which family of server this case is about: eap, datagrid or jvm.

        Decides the installation layout to look for (bin/standalone.sh vs
        bin/server.sh), which *_HOME variables to honour, and whether a server
        is involved at all.
        """
        product = (analysis.product or "").lower()
        if "data grid" in product or "datagrid" in product or "infinispan" in product:
            return "datagrid"
        if "eap" in product or "jboss" in product or "wildfly" in product:
            return "eap"
        # The product line is checked before the symptoms on purpose: an EAP
        # case that ends in OutOfMemoryError is still an EAP case.
        if any(t in product for t in ("jvm", "jdk", "openjdk", "hotspot",
                                      "java se", "java virtual", "temurin")):
            return "jvm"

        # No product named, so classify by what is failing. Heap, GC, metaspace
        # and thread problems reproduce in one JVM and need no server at all.
        hints = ("jvm", "gc", "garbage", "memory", "heap", "oom", "outofmemory",
                 "metaspace", "classloader leak", "thread dump", "deadlock",
                 "thread leak")
        haystack = " ".join(
            [c.lower() for c in analysis.categories]
            + [(analysis.subsystem or "").lower(), (analysis.error_signature or "").lower()]
        )
        if any(h in haystack for h in hints):
            return "jvm"
        return "eap"

    def _home_vars(self, kind: str, major: str) -> list[str]:
        """Env vars to check for the installation, most specific first.

        A product-specific name (EAP8_HOME) is treated as intent; the generic
        EAP_HOME that lives in someone's ~/.bashrc is treated as ambient and is
        skipped rather than fatal when it points at the wrong major version.
        That distinction is what lets one shell profile serve all four
        reproducer workspaces.
        """
        if kind == "datagrid":
            return ["DATAGRID_HOME", "RHDG_HOME", "INFINISPAN_HOME", "SERVER_HOME"]
        specific = [f"EAP{major}_HOME"] if major else []
        return specific + ["EAP_HOME", "JBOSS_HOME", "SERVER_HOME"]

    def _gen_nodes_env(self, analysis: IssueAnalysis, server_config: str) -> str:
        """The single source of truth for the topology, sourced by every script."""
        num_nodes = self._effective_nodes(analysis)
        version = analysis.version or ""
        major = version.split(".")[0] if version and version[0].isdigit() else ""
        kind = self._product_kind(analysis)
        home_vars = " ".join(self._home_vars(kind, major))
        lines = [
            "# Topology of this reproducer. Sourced by start-cluster.sh, run-node.sh,",
            "# stop-cluster.sh and run-reproducer.sh so they can never drift apart.",
            "",
            "# The product and version taken from the support case. ensure-server-home.sh",
            "# enforces these: a reproducer built for one major version refuses to run",
            "# against another, instead of silently producing a meaningless result.",
            f'TARGET_PRODUCT="{analysis.product or "JBoss EAP"}"',
            f'TARGET_VERSION="{version}"',
            f'TARGET_MAJOR="{major}"',
            f'TARGET_KIND="{kind}"',
            "",
            "# Installation is looked up in these variables, in order. The first is",
            "# product-specific and a version mismatch there is fatal; the generic",
            "# ones are ambient (~/.bashrc) and are skipped, not fatal, on mismatch.",
            f'HOME_VARS="{home_vars}"',
            "",
            f'SERVER_CONFIG="{server_config}"',
            "",
        ]

        if kind == "datagrid":
            lines += [
                "# Endpoint security is on by default in Data Grid 8; without a user",
                "# every REST call returns 401 and the reproducer looks broken when it",
                "# is only locked. Created per server root by run-node.sh.",
                'DG_USER="admin"',
                'DG_PASS="admin"',
                "",
                "# Endpoints and JGroups both bind here. Loopback keeps the JGroups",
                "# ports predictable (7800 + offset), which is what lets run-node.sh",
                "# list the peers explicitly instead of relying on IP multicast.",
                'BIND_ADDRESS="127.0.0.1"',
                f'ISSUE_LABEL="{(analysis.error_signature or analysis.subsystem or "data loss on node failure")[:80]}"',
                "",
                '# "<name> <port-offset> <endpoint-port> <server-root-name>"',
                "#",
                "# Every node gets its own server root, seeded from the stock server/",
                "# directory. Nodes sharing one root fight over data/, log/ and the",
                "# index files; a port offset alone does not isolate them.",
                "NODES=(",
            ]
            for i in range(1, num_nodes + 1):
                offset = (i - 1) * 100
                lines.append(f'    "node{i} {offset} {11222 + offset} server-node{i}"')
        else:
            lines += [
                '# "<name> <port-offset> <http-port> <mgmt-port> <server-base-dir-name>"',
                "#",
                "# Every node gets its own jboss.server.base.dir, seeded from the stock",
                "# $EAP_HOME/standalone. Instances that share one base dir fight over",
                "# data/, tmp/, log/ and the deployment markers; a port offset alone does",
                "# not isolate them.",
                "NODES=(",
            ]
            for i in range(1, num_nodes + 1):
                offset = (i - 1) * 100
                lines.append(
                    f'    "node{i} {offset} {8080 + offset} {9990 + offset} standalone-node{i}"'
                )
        lines.append(")")
        return "\n".join(lines) + "\n"

    def _gen_start_script(self, analysis: IssueAnalysis, server_config: str) -> str:
        num_nodes = self._effective_nodes(analysis)
        cluster_session = self._is_cluster_session(analysis)
        issue = analysis.error_signature or analysis.subsystem
        next_steps = (
            [
                'echo "Next: start the sticky load balancer, then run the test:"',
                'echo "  ./lb/start-lb.sh    # Apache sticky-session LB on :8000"',
                'echo "  ./test.sh           # drive traffic through the LB + fail a node"',
                'echo "  ./run-reproducer.sh --repeat 5   # do all of it, repeatedly"',
            ]
            if cluster_session
            else ['echo "Run ./test.sh to trigger the issue"']
        )
        cluster_check = (
            [
                'echo ""',
                'echo "Cluster membership (web cache-container, from node1):"',
                '"$EAP_HOME/bin/jboss-cli.sh" --connect --controller=localhost:9990 \\',
                '    --command="/subsystem=infinispan/cache-container=web:read-resource(include-runtime=true)" \\',
                '    2>/dev/null | grep -E "coordinator" || echo "  (could not read membership)"',
            ]
            if num_nodes > 1
            else []
        )
        return _START_CLUSTER_SH \
            .replace("__ISSUE__", issue) \
            .replace("__NUM_NODES__", str(num_nodes)) \
            .replace("__CLUSTER_CHECK__", "\n".join(cluster_check)) \
            .replace("__NEXT_STEPS__", "\n".join(next_steps))

    def _gen_stop_script(self, analysis: IssueAnalysis) -> str:
        return _STOP_CLUSTER_SH

    def _gen_test_script(self, analysis: IssueAnalysis) -> str:
        """Generate issue-specific test scripts based on categories and trigger."""
        cats = analysis.categories
        trigger = analysis.trigger.lower()
        num_nodes = self._effective_nodes(analysis)

        if self._is_cluster_session(analysis) and num_nodes > 1:
            # Self-contained: drive traffic through the bundled sticky LB and fail
            # a node mid-load -- the only way to actually observe session-loss-on-
            # failover locally.
            return self._gen_lb_failover_test(analysis, num_nodes)
        if ("session" in cats or "clustering" in cats) and num_nodes > 1:
            return self._gen_session_failover_test(analysis, num_nodes)
        if "classloading" in cats:
            return self._gen_classloading_test(analysis)
        return self._gen_basic_test(analysis, num_nodes)

    def _gen_lb_failover_test(self, analysis: IssueAnalysis, num_nodes: int) -> str:
        """Sticky-LB failover test. Verified working shape: pin a session via the
        Apache LB, kill the sticky node's JVM mid-load, then poll for the first
        HTTP 200 after failover and inspect whether the session survived."""
        return (
            "#!/bin/bash\n"
            "# Reproduce: session lost on failover (\"redirected to login after node\n"
            "# restart\"), through a real Apache load balancer with sticky sessions.\n"
            "#\n"
            "#   Client --> Apache LB (:8000, sticky JSESSIONID) --> node1/node2/...\n"
            "#\n"
            "# The LB pins a session to one node. We kill that node abruptly while\n"
            "# requests are in-flight; the LB fails the sticky session over to a\n"
            "# survivor. If Infinispan hasn't transferred the session state in time,\n"
            "# the user lands on a node with no session -> isNew=true / cart=null.\n"
            f"# Expected error signature: {analysis.error_signature}\n"
            "set -uo pipefail\n"
            'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"\n'
            'LB="http://localhost:8000/reproducer/session"\n'
            'COOKIE_JAR="/tmp/reproducer-cookies.txt"\n'
            'rm -f "$COOKIE_JAR"\n'
            "REPRODUCED=0\n\n"
            "# Fail fast if the load balancer isn't up.\n"
            'if ! curl -s -o /dev/null "http://localhost:8000/balancer-manager"; then\n'
            '    echo "ERROR: load balancer not reachable on :8000. Run ./lb/start-lb.sh first."\n'
            "    exit 1\n"
            "fi\n\n"
            "hit() {  # hit [querystring] -> servlet body, via the load balancer\n"
            "    local qs=${1:-}\n"
            '    curl -s -c "$COOKIE_JAR" -b "$COOKIE_JAR" "${LB}${qs:+?$qs}"\n'
            "}\n"
            "field() { echo \"$1\" | grep \"^$2=\" | cut -d= -f2- | tr -d '\\r'; }\n\n"
            "start_load() {  # in-flight (delayed) requests to the sticky node\n"
            "    for _ in $(seq 1 30); do\n"
            '        curl -s -c "$COOKIE_JAR" -b "$COOKIE_JAR" "${LB}?delay=800" > /dev/null 2>&1 &\n'
            "    done\n"
            "}\n\n"
            "kill_node() {  # kill_node <node> -> abrupt kill -9 of the actual EAP JVM\n"
            "    local node=$1\n"
            "    # Target the JVM by its jboss.node.name (the .pid file holds the\n"
            "    # standalone.sh wrapper PID, not the java child).\n"
            '    local pids; pids=$(pgrep -f "jboss.node.name=$node" || true)\n'
            '    if [ -n "$pids" ]; then\n'
            '        echo "   >>> kill -9 $node (JVM PIDs: $pids) MID-LOAD"\n'
            "        kill -9 $pids 2>/dev/null || true\n"
            "    else\n"
            '        echo "   !! no running JVM found for $node"\n'
            "    fi\n"
            "}\n\n"
            'echo "=========================================================="\n'
            'echo "  Session Failover Reproducer (Apache LB + sticky sessions)"\n'
            'echo "=========================================================="\n\n'
            'echo "Step 1: Establish a session through the LB and fill the cart..."\n'
            'for item in laptop mouse keyboard; do hit "item=$item" > /dev/null; done\n'
            "BODY=$(hit)\n"
            'PRIMARY=$(field "$BODY" node)\n'
            'echo "   LB pinned session to: $PRIMARY"\n'
            'echo "   counter=$(field "$BODY" counter) cart=$(field "$BODY" cart) sessionId=$(field "$BODY" sessionId)"\n\n'
            'echo "Step 2: Kill the sticky node abruptly under load; LB must fail over..."\n'
            "for round in 1 2; do\n"
            '    BODY=$(hit); CUR=$(field "$BODY" node)\n'
            '    if [ -z "$CUR" ]; then echo "--- Round $round: no live sticky node, stopping ---"; break; fi\n'
            '    echo ""; echo "--- Round $round: sticky node = $CUR ---"\n'
            "    start_load\n"
            "    sleep 0.3\n"
            '    kill_node "$CUR"\n'
            "    # A real user retries during failover. Poll up to ~8s for the first 200.\n"
            '    AFTER=""; status=""\n'
            "    for _ in $(seq 1 16); do\n"
            "        resp=$(curl -s -w '\\n%{http_code}' -c \"$COOKIE_JAR\" -b \"$COOKIE_JAR\" \"$LB\" 2>/dev/null)\n"
            '        status=$(echo "$resp" | tail -n1)\n'
            "        AFTER=$(echo \"$resp\" | sed '$d')\n"
            '        [ "$status" = "200" ] && break\n'
            "        sleep 0.5\n"
            "    done\n"
            "    wait 2>/dev/null || true\n"
            '    onnode=$(field "$AFTER" node); isnew=$(field "$AFTER" isNew)\n'
            '    counter=$(field "$AFTER" counter); cart=$(field "$AFTER" cart)\n'
            '    echo "   first 200 after failover: node=$onnode isNew=$isnew counter=$counter cart=$cart (last http=$status)"\n'
            '    if [ "$status" != "200" ]; then\n'
            '        echo "   [MODE A] LB never recovered the sticky session -> persistent error (failover gap)."\n'
            "        REPRODUCED=1\n"
            '    elif [ "$isnew" = "true" ] || [ "$counter" = "1" ] || [ "$cart" = "null" ]; then\n'
            '        echo "   [MODE B] Failover SUCCEEDED but SESSION LOST -> login redirect (the customer bug)."\n'
            "        REPRODUCED=1\n"
            "    else\n"
            '        echo "   [OK] Failover succeeded on $onnode and session survived (replication works)."\n'
            "    fi\n"
            "done\n\n"
            'echo ""; echo "=========================================================="\n'
            'if [ "$REPRODUCED" -eq 1 ]; then\n'
            '    echo "  *** ISSUE REPRODUCED: session lost / failover error ***"\n'
            "else\n"
            '    echo "  Issue NOT reproduced this run (intermittent)."\n'
            '    echo "  Re-run ./stop-cluster.sh && ./start-cluster.sh && ./test.sh."\n'
            '    echo "  If it never reproduces on this build, install the exact"\n'
            f'    echo "  customer micro-version ({analysis.version or "e.g. the regressed patch level"})."\n'
            "fi\n"
            'echo "=========================================================="\n\n'
            'echo "LB access log (which node served each request):"\n'
            'tail -n 8 "$SCRIPT_DIR/lb/access.log" 2>/dev/null || echo "  (no LB log)"\n'
            'rm -f "$COOKIE_JAR"\n'
        )

    def _gen_session_failover_test(self, analysis: IssueAnalysis, num_nodes: int) -> str:
        node1_port = 8080
        node2_port = 8180
        lines = [
            '#!/bin/bash',
            '# Test: Session failover / replication issue',
            f'# Expected error: {analysis.error_signature}',
            f'# Trigger: {analysis.trigger}',
            'set -euo pipefail',
            '',
            f'NODE1="http://localhost:{node1_port}"',
            f'NODE2="http://localhost:{node2_port}"',
            'COOKIE_JAR="/tmp/reproducer-cookies.txt"',
            'EAP_HOME="${EAP_HOME:?Set EAP_HOME}"',
            '',
            'rm -f "$COOKIE_JAR"',
            '',
            'echo "=========================================="',
            'echo "  Session Failover Reproducer Test"',
            'echo "=========================================="',
            '',
            '# Step 1: Create session on node1',
            'echo ""',
            'echo "Step 1: Creating session on node1..."',
            'curl -s -c "$COOKIE_JAR" "$NODE1/reproducer/session?item=laptop"',
            'echo ""',
            '',
            '# Step 2: Verify session counter increments on same node',
            'echo "Step 2: Incrementing session counter on node1..."',
            'for i in $(seq 1 5); do',
            '    curl -s -b "$COOKIE_JAR" -c "$COOKIE_JAR" "$NODE1/reproducer/session?item=item$i"',
            '    echo ""',
            'done',
            '',
            '# Step 3: Verify session is accessible from node2 (before failover)',
            'echo ""',
            'echo "Step 3: Checking session on node2 (should see replicated data)..."',
            'curl -s -b "$COOKIE_JAR" -c "$COOKIE_JAR" "$NODE2/reproducer/session"',
            'echo ""',
            '',
            '# Step 4: Simulate node failure (restart node1)',
            'echo ""',
            'echo "Step 4: Restarting node1 to trigger failover..."',
            f'"$EAP_HOME/bin/jboss-cli.sh" --connect --controller=localhost:9990 \\',
            '    --command=":shutdown" 2>/dev/null || true',
            'echo "node1 stopped. Waiting 5 seconds..."',
            'sleep 5',
            '',
            '# Step 5: Access session on node2 (failover)',
            'echo ""',
            'echo "Step 5: Accessing session on node2 after node1 shutdown..."',
            'echo ">>> If the issue reproduces, you will see session data LOST <<<"',
            'RESULT=$(curl -s -b "$COOKIE_JAR" -c "$COOKIE_JAR" "$NODE2/reproducer/session")',
            'echo "$RESULT"',
            '',
            '# Check if session was preserved',
            'if echo "$RESULT" | grep -q "isNew=true"; then',
            '    echo ""',
            '    echo "*** ISSUE REPRODUCED: Session was LOST after failover ***"',
            '    echo "*** Counter reset and cart data missing ***"',
            'else',
            '    echo ""',
            '    echo "Session survived failover (counter preserved)."',
            '    echo "Issue NOT reproduced - session replication is working."',
            'fi',
            '',
            '# Step 6: Send concurrent requests to stress replication',
            'echo ""',
            'echo "Step 6: Sending concurrent requests to stress test..."',
            'for i in $(seq 1 20); do',
            f'    curl -s -b "$COOKIE_JAR" -c "$COOKIE_JAR" "$NODE2/reproducer/session" > /dev/null &',
            'done',
            'wait',
            'echo "Concurrent requests complete."',
            '',
            '# Step 7: Check server logs',
            'echo ""',
            'echo "Step 7: Checking logs for errors..."',
            'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"',
            'for f in "$SCRIPT_DIR"/node*.log; do',
            '    if [ -f "$f" ]; then',
            '        ERRORS=$(grep -c "ERROR\\|WARN.*ISPN\\|IllegalStateException\\|Session.*invalid" "$f" 2>/dev/null || true)',
            '        echo "  $(basename $f): $ERRORS error/warning lines"',
            '    fi',
            'done',
            '',
            'echo ""',
            'echo "Done. Review node*.log files for detailed errors."',
            '',
            'rm -f "$COOKIE_JAR"',
        ]
        return '\n'.join(lines) + '\n'

    def _gen_classloading_test(self, analysis: IssueAnalysis) -> str:
        return (
            '#!/bin/bash\n'
            '# Test: Classloading / deployment issue\n'
            f'# Expected error: {analysis.error_signature}\n'
            'set -euo pipefail\n\n'
            'BASE_URL="${1:-http://localhost:8080/reproducer}"\n\n'
            'echo "Testing classloading..."\n'
            'echo "Checking javax.servlet.http.HttpServlet:"\n'
            'curl -s "$BASE_URL/test?class=javax.servlet.http.HttpServlet"\n'
            'echo ""\n'
            'echo "Checking jakarta.servlet.http.HttpServlet:"\n'
            'curl -s "$BASE_URL/test?class=jakarta.servlet.http.HttpServlet"\n'
            'echo ""\n'
            'echo "Check server log for ClassNotFoundException"\n'
        )

    def _gen_basic_test(self, analysis: IssueAnalysis, num_nodes: int) -> str:
        lines = [
            '#!/bin/bash',
            f'# Test: {analysis.subsystem} issue',
            f'# Expected error: {analysis.error_signature}',
            f'# Trigger: {analysis.trigger}',
            'set -euo pipefail',
            '',
        ]
        for i in range(1, num_nodes + 1):
            port = 8080 + (i - 1) * 100
            lines += [
                f'echo "Testing node{i} (port {port})..."',
                f'curl -s "http://localhost:{port}/reproducer/test"',
                'echo ""',
            ]
        lines += [
            '',
            'echo "Check server logs for expected errors."',
        ]
        return '\n'.join(lines) + '\n'

    def _gen_cli_script(self, analysis: IssueAnalysis, server_config: str) -> str:
        lines = [
            f'# JBoss CLI configuration for {analysis.subsystem} reproducer',
            f'# Server config: {server_config}',
            '',
        ]

        subsys = analysis.subsystem.lower()
        cats = analysis.categories

        if subsys == "infinispan" or "session" in cats or "clustering" in cats:
            lines += [
                '# Enable infinispan statistics for debugging',
                '/subsystem=infinispan/cache-container=web:write-attribute(name=statistics-enabled,value=true)',
                '/subsystem=infinispan/cache-container=web/distributed-cache=dist:write-attribute(name=statistics-enabled,value=true)',
                '',
                '# Enable clustering debug logging',
                '/subsystem=logging/logger=org.infinispan:add(level=DEBUG)',
                '/subsystem=logging/logger=org.jgroups:add(level=DEBUG)',
            ]
        elif subsys == "undertow":
            lines += [
                '/subsystem=undertow/server=default-server/http-listener=default:write-attribute(name=max-connections,value=200)',
                '/subsystem=logging/logger=io.undertow:add(level=DEBUG)',
            ]
        elif "elytron" in subsys:
            lines += [
                '/subsystem=logging/logger=org.wildfly.security:add(level=DEBUG)',
            ]
        elif "datasource" in subsys:
            lines += [
                '/subsystem=logging/logger=org.jboss.jca:add(level=DEBUG)',
            ]
        else:
            lines += [
                f'# Add {analysis.subsystem}-specific configuration as needed',
                f'/subsystem=logging/logger=com.redhat.reproducer:add(level=DEBUG)',
            ]

        return '\n'.join(lines) + '\n'

    # --- Self-contained clustering: load balancer + instance-id ---

    def _is_cluster_session(self, analysis: IssueAnalysis) -> bool:
        cats = analysis.categories
        return any(c in cats for c in ["clustering", "session", "cross-site"]) \
            or analysis.subsystem.lower() in ("infinispan", "jgroups")

    def _effective_nodes(self, analysis: IssueAnalysis) -> int:
        """Nodes to actually start. Clustering/session needs >=3 so that, with the
        web cache's default owners=2, a session is not present on every node and a
        failover can hit a node that must fetch state."""
        num_nodes = max(analysis.num_nodes, 1)
        if self._is_cluster_session(analysis):
            return max(num_nodes, 3)
        return num_nodes

    def _gen_instance_id_cli(self) -> str:
        return (
            "# Make Undertow append the node's route to JSESSIONID (e.g. <id>.node1).\n"
            "# This is what lets the Apache balancer do sticky routing + failover.\n"
            "# Applied per node; ${jboss.node.name} resolves to node1/node2/... at runtime.\n"
            "/subsystem=undertow:write-attribute(name=instance-id, value=${jboss.node.name})\n"
            ":reload\n"
        )

    def _gen_lb_config(self, num_nodes: int) -> str:
        members = "\n".join(
            f"    BalancerMember http://localhost:{8080 + (i - 1) * 100} "
            f"route=node{i} retry=5"
            for i in range(1, num_nodes + 1)
        )
        return (
            "# Self-contained Apache httpd load balancer for the clustering reproducer.\n"
            "# Provides STICKY SESSIONS by JSESSIONID route suffix + automatic failover\n"
            "# to a surviving node -- the same semantics as the customer's mod_cluster.\n"
            "# Run from start-lb.sh; __LBDIR__ is replaced with the absolute lb/ path.\n\n"
            'ServerRoot "/etc/httpd"\n'
            "ServerName localhost\n"
            "Listen 8000\n\n"
            "LoadModule mpm_event_module        /usr/lib64/httpd/modules/mod_mpm_event.so\n"
            "LoadModule unixd_module            /usr/lib64/httpd/modules/mod_unixd.so\n"
            "LoadModule authz_core_module       /usr/lib64/httpd/modules/mod_authz_core.so\n"
            "LoadModule log_config_module       /usr/lib64/httpd/modules/mod_log_config.so\n"
            "LoadModule headers_module          /usr/lib64/httpd/modules/mod_headers.so\n"
            "LoadModule proxy_module            /usr/lib64/httpd/modules/mod_proxy.so\n"
            "LoadModule proxy_http_module       /usr/lib64/httpd/modules/mod_proxy_http.so\n"
            "LoadModule proxy_balancer_module   /usr/lib64/httpd/modules/mod_proxy_balancer.so\n"
            "LoadModule lbmethod_byrequests_module /usr/lib64/httpd/modules/mod_lbmethod_byrequests.so\n"
            "LoadModule slotmem_shm_module      /usr/lib64/httpd/modules/mod_slotmem_shm.so\n\n"
            'PidFile            "__LBDIR__/httpd.pid"\n'
            'DefaultRuntimeDir  "__LBDIR__/runtime"\n'
            'ErrorLog           "__LBDIR__/error.log"\n'
            'LogFormat "%h %t \\"%r\\" %>s route=%{BALANCER_WORKER_ROUTE}e '
            'sticky=%{BALANCER_SESSION_STICKY}e" lb\n'
            'CustomLog          "__LBDIR__/access.log" lb\n\n'
            "# Fail a node fast so failover is visible during the test.\n"
            "ProxyTimeout 10\n\n"
            "<Proxy balancer://eapcluster>\n"
            f"{members}\n"
            "    ProxySet stickysession=JSESSIONID\n"
            "</Proxy>\n\n"
            "ProxyPass        /reproducer balancer://eapcluster/reproducer\n"
            "ProxyPassReverse /reproducer balancer://eapcluster/reproducer\n\n"
            '<Location "/balancer-manager">\n'
            "    SetHandler balancer-manager\n"
            "    Require all granted\n"
            "</Location>\n"
        )

    def _gen_lb_scripts(self) -> dict[str, str]:
        start_lb = (
            "#!/bin/bash\n"
            "# Start the Apache httpd sticky-session load balancer in front of the nodes.\n"
            "set -euo pipefail\n"
            'LBDIR="$(cd "$(dirname "$0")" && pwd)"\n'
            'mkdir -p "$LBDIR/runtime"\n\n'
            "# Materialize a config with absolute paths.\n"
            'sed "s#__LBDIR__#$LBDIR#g" "$LBDIR/httpd.conf" > "$LBDIR/httpd.runtime.conf"\n\n'
            "# Validate config first (fails loudly instead of half-starting).\n"
            'httpd -f "$LBDIR/httpd.runtime.conf" -t\n\n'
            'echo "Starting httpd load balancer on http://localhost:8000 ..."\n'
            'httpd -f "$LBDIR/httpd.runtime.conf" -k start\n'
            "sleep 1\n"
            'if [ -f "$LBDIR/httpd.pid" ] && kill -0 "$(cat "$LBDIR/httpd.pid")" 2>/dev/null; then\n'
            '    echo "Load balancer RUNNING (PID $(cat "$LBDIR/httpd.pid"))"\n'
            '    echo "  App via LB:        http://localhost:8000/reproducer/session"\n'
            '    echo "  Balancer manager:  http://localhost:8000/balancer-manager"\n'
            "else\n"
            '    echo "ERROR: load balancer failed to start. See $LBDIR/error.log"; exit 1\n'
            "fi\n"
        )
        stop_lb = (
            "#!/bin/bash\n"
            "# Stop the httpd load balancer.\n"
            "set -uo pipefail\n"
            'LBDIR="$(cd "$(dirname "$0")" && pwd)"\n'
            'if [ -f "$LBDIR/httpd.runtime.conf" ]; then\n'
            '    httpd -f "$LBDIR/httpd.runtime.conf" -k stop 2>/dev/null || true\n'
            "fi\n"
            'if [ -f "$LBDIR/httpd.pid" ]; then\n'
            '    kill "$(cat "$LBDIR/httpd.pid")" 2>/dev/null || true\n'
            '    rm -f "$LBDIR/httpd.pid"\n'
            "fi\n"
            'echo "Load balancer stopped."\n'
        )
        return {"lb/start-lb.sh": start_lb, "lb/stop-lb.sh": stop_lb}

    def _gen_repro_requirements_section(self, analysis: IssueAnalysis) -> list[str]:
        """Render the honest "what it takes to reproduce" section so the report
        never silently over-claims. Confidence + explicit requirement list, each
        marked as bundled by this reproducer or still needed from the user."""
        conf = (analysis.reproduction_confidence or "").lower()
        conf_label = {
            "high": "HIGH -- this reproducer should trigger the issue directly.",
            "partial": "PARTIAL -- reproducer sets up the conditions, but the bug "
                       "is timing/version sensitive and may not fire every run.",
            "low": "LOW -- likely needs the exact customer build/artifacts below; "
                   "the reproducer establishes the topology but cannot guarantee it.",
        }.get(conf, "UNKNOWN -- see requirements below.")

        reqs = list(analysis.reproduction_requirements)
        if not reqs:
            reqs = ["No special prerequisites detected beyond the environment above."]

        # Bundled = things this reproducer now provides itself.
        bundled = []
        if self._is_cluster_session(analysis):
            bundled = [
                f"{self._effective_nodes(analysis)}-node HA cluster (start-cluster.sh)",
                "Apache sticky-session load balancer (lb/start-lb.sh)",
                "Undertow instance-id / JSESSIONID routing (instance-id.cli)",
                "Concurrent in-flight load + abrupt node kill (test.sh)",
            ]

        lines = [
            '## Reproduction Requirements & Gap Analysis',
            '',
            f'**Reproduction confidence:** {conf_label}',
            '',
            'To reproduce this issue reliably, the following are required:',
            '',
        ]
        lines += [f'- [ ] {r}' for r in reqs]
        if bundled:
            lines += [
                '',
                '**Provided by this reproducer (already satisfied):**',
                '',
            ]
            lines += [f'- [x] {b}' for b in bundled]
        lines += [
            '',
            '> Any item left unchecked above must be supplied from the customer '
            'environment (exact build, JVM flags, heap/thread dumps, or workload) '
            'for a faithful reproduction.',
            '',
        ]
        return lines

    def _gen_readme(
        self, analysis: IssueAnalysis, server_config: str, config: ReproducerConfig,
    ) -> str:
        num_nodes = self._effective_nodes(analysis)
        cluster_session = self._is_cluster_session(analysis)
        has_ocp = config.openshift_reproducer and self._needs_openshift(analysis)
        product = analysis.product or "JBoss EAP"
        version = analysis.version or "unknown version"
        namespace_word = self._determine_namespace(analysis)

        lines = [
            f'# Reproducer: {analysis.error_signature or analysis.subsystem} Issue',
            '',
            '## Issue Summary',
            '',
            f'- **Product:** {analysis.product} {analysis.version}',
            f'- **Subsystem:** {analysis.subsystem}',
            f'- **Categories:** {", ".join(analysis.categories)}',
            f'- **Server Config:** {server_config}',
            f'- **Nodes:** {num_nodes}',
        ]
        if analysis.error_signature:
            lines.append(f'- **Error:** `{analysis.error_signature}`')
        if analysis.possible_root_cause:
            lines.append(f'- **Root Cause:** {analysis.possible_root_cause}')
        lines.append('')
        lines += self._gen_repro_requirements_section(analysis)
        lines += [
            '## Environment Requirements',
            '',
            f'- {analysis.product} {analysis.version}',
            f'- JDK: {analysis.jdk or "OpenJDK 11+"}',
            f'- OS: {analysis.os or "RHEL 8/9"}',
            '- Maven 3.8+',
            '',
            '## Topology',
            '',
            f'{analysis.topology_description}',
            '',
            '```',
        ]
        if num_nodes > 1:
            lines.append('Client')
            lines.append('  |')
            node_line = ' ---- '.join([f'EAP Node {i}' for i in range(1, num_nodes + 1)])
            lines.append(f'  v')
            lines.append(node_line)
            for i in range(1, num_nodes + 1):
                offset = (i - 1) * 100
                lines.append(f'  node{i}: HTTP={8080+offset}, mgmt={9990+offset}')
        else:
            lines.append('Client --> EAP Node 1 (HTTP=8080)')
        lines += [
            '```',
            '',
            '## Quick Start (one command)',
            '',
            '```bash',
            './run-reproducer.sh --repeat 5',
            '```',
            '',
            f'You do not set `EAP_HOME`. This package was generated from a case that',
            f'says **{product} {version}**, and `ensure-server-home.sh` finds an',
            f'installation of that major version on this machine and uses it. If',
            f'`EAP_HOME` is already set to a different major version the run **aborts**',
            f'rather than producing a meaningless result -- a {namespace_word} application',
            f'on the other major version deploys "OK" and then 404s on every request.',
            f'Point it at a non-standard location with',
            f'`EAP_SEARCH_PATHS=/where/i/keep/eap`.',
            '',
            'That provisions a compatible JDK (downloading one if this machine has',
            f'none), builds the app, brings up all {num_nodes} nodes **each in its own',
            'terminal window**, starts the load balancer, runs the test and prints a',
            'verdict. `--repeat` re-runs the whole cycle, which matters because the',
            'failure is intermittent. Add `--no-terminals` over SSH or in CI.',
            '',
            'You do not need to set `JAVA_HOME`. `ensure-jdk.sh` reads',
            '`$EAP_HOME/version.txt` and picks a JDK the server can actually boot:',
            'JDK 8-11 for EAP 7.x (the legacy `security` subsystem and',
            '`<security-realms>` in the stock configs are rejected on JDK 14+, so the',
            'server dies at boot with WFLYSRV0056), JDK 17-21 for EAP 8.x. If nothing',
            'suitable is installed it fetches a Temurin build into `~/jdks`.',
            '',
            '## Steps to Reproduce (manual)',
            '',
            '1. Check which installation will be used:',
            '   ```bash',
            '   ./ensure-server-home.sh          # prints the version it picked, or why it refused',
            '   ```',
            '',
            '2. Build the application:',
            '   ```bash',
            '   chmod +x *.sh',
            '   ./build.sh',
            '   ```',
            '',
            f'3. Start the {num_nodes}-node cluster (one terminal window per node):',
            '   ```bash',
            '   ./start-cluster.sh',
            '   # or add a single node by hand:',
            '   ./run-node.sh node2 100 standalone-node2',
            '   ```',
            '',
        ]
        if cluster_session:
            lines += [
                '4. Start the sticky load balancer (bundled):',
                '   ```bash',
                '   ./lb/start-lb.sh   # Apache mod_proxy_balancer on :8000',
                '   ```',
                '',
                '5. Trigger the issue (drives load through the LB, kills a node):',
                '   ```bash',
                '   ./test.sh',
                '   ```',
                '',
                '6. Tear down:',
                '   ```bash',
                '   ./lb/stop-lb.sh',
                '   ./stop-cluster.sh',
                '   ```',
                '',
            ]
        else:
            lines += [
                '4. Trigger the issue:',
                '   ```bash',
                '   ./test.sh',
                '   ```',
                '',
                '5. Stop the cluster:',
                '   ```bash',
                '   ./stop-cluster.sh',
                '   ```',
                '',
            ]
        lines += [
            '## Expected vs Actual Behavior',
            '',
        ]
        if analysis.expected_behavior:
            lines.append(f'**Expected:** {analysis.expected_behavior}')
        if analysis.actual_behavior:
            lines.append(f'**Actual:** {analysis.actual_behavior}')
        if analysis.error_signature:
            lines += [
                '',
                '## Expected Error',
                '',
                '```',
                analysis.error_signature,
                '```',
            ]
        lines += [
            '',
            '## Log Locations',
            '',
        ]
        for i in range(1, num_nodes + 1):
            lines.append(f'- node{i}: `./node{i}.log` and `$EAP_HOME/standalone/log/server.log`')
        lines += [
            '',
            '## Validation Status',
            '',
            'NOT EXECUTED -- generated based on analysis',
        ]
        return '\n'.join(lines) + '\n'

    # --- OpenShift ---

    def _gen_openshift(self, analysis: IssueAnalysis) -> dict[str, str]:
        num_nodes = max(analysis.num_nodes, 1)
        app_name = "reproducer"

        deployment = (
            f'apiVersion: apps/v1\n'
            f'kind: Deployment\n'
            f'metadata:\n'
            f'  name: {app_name}\n'
            f'  labels:\n'
            f'    app: {app_name}\n'
            f'spec:\n'
            f'  replicas: {num_nodes}\n'
            f'  selector:\n'
            f'    matchLabels:\n'
            f'      app: {app_name}\n'
            f'  template:\n'
            f'    metadata:\n'
            f'      labels:\n'
            f'        app: {app_name}\n'
            f'    spec:\n'
            f'      containers:\n'
            f'      - name: eap\n'
            f'        image: registry.redhat.io/jboss-eap-7/eap74-openjdk11-openshift-rhel8:latest\n'
            f'        ports:\n'
            f'        - containerPort: 8080\n'
            f'          name: http\n'
            f'        - containerPort: 8888\n'
            f'          name: ping\n'
            f'        env:\n'
            f'        - name: JGROUPS_PING_PROTOCOL\n'
            f'          value: dns.DNS_PING\n'
            f'        - name: OPENSHIFT_DNS_PING_SERVICE_NAME\n'
            f'          value: {app_name}-ping\n'
            f'        - name: OPENSHIFT_DNS_PING_SERVICE_PORT\n'
            f'          value: "8888"\n'
            f'        resources:\n'
            f'          requests:\n'
            f'            memory: 512Mi\n'
            f'            cpu: 500m\n'
            f'          limits:\n'
            f'            memory: 1Gi\n'
            f'            cpu: "1"\n'
            f'        readinessProbe:\n'
            f'          httpGet:\n'
            f'            path: /reproducer/session\n'
            f'            port: 8080\n'
            f'          initialDelaySeconds: 30\n'
        )

        service = (
            f'apiVersion: v1\n'
            f'kind: Service\n'
            f'metadata:\n'
            f'  name: {app_name}\n'
            f'spec:\n'
            f'  selector:\n'
            f'    app: {app_name}\n'
            f'  ports:\n'
            f'  - name: http\n'
            f'    port: 8080\n'
            f'    targetPort: 8080\n'
        )

        ping_service = (
            f'apiVersion: v1\n'
            f'kind: Service\n'
            f'metadata:\n'
            f'  name: {app_name}-ping\n'
            f'  annotations:\n'
            f'    service.alpha.kubernetes.io/tolerate-unready-endpoints: "true"\n'
            f'spec:\n'
            f'  clusterIP: None\n'
            f'  selector:\n'
            f'    app: {app_name}\n'
            f'  ports:\n'
            f'  - name: ping\n'
            f'    port: 8888\n'
        )

        route = (
            f'apiVersion: route.openshift.io/v1\n'
            f'kind: Route\n'
            f'metadata:\n'
            f'  name: {app_name}\n'
            f'spec:\n'
            f'  to:\n'
            f'    kind: Service\n'
            f'    name: {app_name}\n'
            f'  port:\n'
            f'    targetPort: http\n'
        )

        return {
            "openshift/deployment.yaml": deployment,
            "openshift/service.yaml": service,
            "openshift/ping-service.yaml": ping_service,
            "openshift/route.yaml": route,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _determine_namespace(self, analysis: IssueAnalysis) -> str:
        product = analysis.product.lower()
        if "eap" in product:
            try:
                major = int(analysis.version.split(".")[0])
                if major >= 8:
                    return "jakarta"
            except (ValueError, IndexError):
                pass
            return "javax"
        return "jakarta"

    def _determine_server_config(self, analysis: IssueAnalysis) -> str:
        # Data Grid Server has no standalone*.xml at all -- its configs live in
        # server/conf/ and are named infinispan.xml.
        kind = self._product_kind(analysis)
        if kind == "datagrid":
            return "infinispan.xml"
        if kind == "jvm":
            return ""

        cats = [c.lower() for c in analysis.categories]
        dt = analysis.deployment_type.lower()
        all_text = (analysis.reproducer_strategy + " " + analysis.topology_description + " " + dt).lower()

        needs_ha = (
            analysis.num_nodes > 1
            or "clustering" in cats or "session" in cats or "cross-site" in cats
            or "ha" in dt or "cluster" in dt
            or "jgroups" in analysis.subsystem.lower() or "infinispan" in analysis.subsystem.lower()
        )
        needs_messaging = "messaging" in cats

        if needs_messaging and needs_ha:
            return "standalone-full-ha.xml"
        if needs_messaging:
            return "standalone-full.xml"
        if needs_ha:
            return "standalone-ha.xml"
        return "standalone.xml"

    def _needs_openshift(self, analysis: IssueAnalysis) -> bool:
        dt = analysis.deployment_type.lower()
        return "openshift" in dt or "ocp" in dt or "kubernetes" in dt

    def _parse_file_map(self, content: str) -> dict[str, str]:
        if not content:
            return {}
        fence_match = re.search(r"```(?:json)?\s*\n(.*?)```", content, re.DOTALL)
        json_str = fence_match.group(1) if fence_match else content
        brace_start = json_str.find("{")
        if brace_start >= 0:
            try:
                data = json.loads(json_str[brace_start:])
                if isinstance(data, dict):
                    return {k: str(v) for k, v in data.items()}
            except json.JSONDecodeError:
                pass
        return {}

    def _write_files(self, base_dir: Path, files: dict[str, str]) -> None:
        for rel_path, content in files.items():
            file_path = base_dir / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            if rel_path.endswith(".sh"):
                file_path.chmod(0o755)
