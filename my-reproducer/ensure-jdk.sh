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
    "$1" -version 2>&1 | head -1 \
        | sed -e 's/.*version "\([0-9]*\)\.\([0-9]*\).*/\1 \2/' \
        | awk '{ if ($1 == 1) print $2; else print $1 }'
}

# --- Which JDK does THIS EAP need? -----------------------------------------
EAP_VER="$(grep -oE '[0-9]+\.[0-9]+' "$EAP_HOME/version.txt" 2>/dev/null | head -1)"
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
printf 'export JAVA_HOME=%s\n' "$RESOLVED" > "$ENV_FILE"

if [ "${1:-}" = "--export" ]; then
    printf 'export JAVA_HOME=%s\n' "$RESOLVED"
fi
