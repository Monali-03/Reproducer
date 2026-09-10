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
EAP_HOME="${EAP_HOME:?ERROR: set EAP_HOME so the required JDK can be determined}"

log() { echo "[ensure-jdk] $*" >&2; }

java_major() {  # java_major <path-to-java> -> 8, 11, 17, 21, ...
    "$1" -version 2>&1 | head -1 \\
        | sed -e 's/.*version "\\([0-9]*\\)\\.\\([0-9]*\\).*/\\1 \\2/' \\
        | awk '{ if ($1 == 1) print $2; else print $1 }'
}

# --- Which JDK does THIS EAP need? -----------------------------------------
EAP_VER="$(grep -oE '[0-9]+\\.[0-9]+' "$EAP_HOME/version.txt" 2>/dev/null | head -1)"
case "${EAP_VER%%.*}" in
    7)  JDK_MIN=8;  JDK_MAX=11; JDK_WANT=11
        REASON="EAP $EAP_VER: legacy security subsystem/realms are rejected on JDK 14+" ;;
    8)  JDK_MIN=17; JDK_MAX=21; JDK_WANT=17
        REASON="EAP $EAP_VER: Jakarta EE 10 requires JDK 17 or 21" ;;
    *)  JDK_MIN=17; JDK_MAX=21; JDK_WANT=17
        REASON="unknown EAP version at $EAP_HOME; assuming a modern release" ;;
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

EAP_HOME="${EAP_HOME:?ERROR: Set EAP_HOME to your JBoss EAP installation}"
BASE="$EAP_HOME/$BASEDIR_NAME"

# A terminal emulator started as a D-Bus service (gnome-terminal, ptyxis) does
# NOT inherit the launcher's environment, so resolve the JDK here too.
if [ -z "${JAVA_HOME:-}" ]; then
    eval "$("$SCRIPT_DIR/ensure-jdk.sh" --export)"
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

export EAP_HOME="${EAP_HOME:?ERROR: Set EAP_HOME to your JBoss EAP installation}"
eval "$("$SCRIPT_DIR/ensure-jdk.sh" --export)"
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
EAP_HOME="${EAP_HOME:?ERROR: Set EAP_HOME to your JBoss EAP installation}"

USE_TERMINALS=1
[ "${1:-}" = "--no-terminals" ] && USE_TERMINALS=0

if [ ! -f "$EAP_HOME/bin/standalone.sh" ]; then
    echo "ERROR: $EAP_HOME/bin/standalone.sh not found"
    exit 1
fi

eval "$("$SCRIPT_DIR/ensure-jdk.sh" --export)"
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
EAP_HOME="${EAP_HOME:-}"

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

        # 8. README
        files["README.md"] = self._gen_readme(analysis, server_config, config)

        # 9. OpenShift files if applicable
        if config.openshift_reproducer and self._needs_openshift(analysis):
            files.update(self._gen_openshift(analysis))

        self._write_files(output_dir, files)

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

    def _gen_nodes_env(self, analysis: IssueAnalysis, server_config: str) -> str:
        """The single source of truth for the topology, sourced by every script."""
        num_nodes = self._effective_nodes(analysis)
        lines = [
            "# Topology of this reproducer. Sourced by start-cluster.sh, run-node.sh,",
            "# stop-cluster.sh and run-reproducer.sh so they can never drift apart.",
            f'SERVER_CONFIG="{server_config}"',
            "",
            "# \"<name> <port-offset> <http-port> <mgmt-port> <server-base-dir-name>\"",
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
            'export EAP_HOME=/path/to/jboss-eap',
            './run-reproducer.sh --repeat 5',
            '```',
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
            '1. Set environment:',
            '   ```bash',
            '   export EAP_HOME=/path/to/jboss-eap',
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
