#!/bin/bash
# Resolve the EAP installation this reproducer is FOR, and refuse to run against
# any other one. Prints "export EAP_HOME=..." on stdout, progress on stderr:
#
#     eval "$(./ensure-eap-home.sh --export)"
#
# Why this exists: the target product/version comes from the support case, but
# EAP_HOME comes from whoever's shell happens to be running the script. Nothing
# used to connect the two, so a reproducer generated from an EAP 8 case would
# silently deploy onto an EAP 7 server (jakarta app on a javax server: the war
# deploys "OK" and every servlet 404s) or the other way round. The case file
# decides; if the environment disagrees, that is an error, not a warning.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/nodes.env"

log() { echo "[ensure-eap-home] $*" >&2; }

installed_version() {  # installed_version <eap-home> -> e.g. 7.4.23.GA
    grep -oE '[0-9]+\.[0-9]+[0-9.A-Za-z]*' "$1/version.txt" 2>/dev/null | head -1
}

# Every EAP installation under the usual lab locations.
find_installs() {
    local root s
    for root in ${EAP_SEARCH_PATHS:-} "$HOME/Documents/EAP_lab" "$HOME/EAP" \
                "$HOME/eap" "$HOME/jboss" /opt/jboss /opt/eap /opt; do
        [ -d "$root" ] || continue
        while IFS= read -r s; do
            [ -n "$s" ] && dirname "$(dirname "$s")"
        done < <(find "$root" -maxdepth 4 -path '*/bin/standalone.sh' 2>/dev/null)
    done | sort -u
}

report_candidates() {
    local h v
    while IFS= read -r h; do
        [ -n "$h" ] || continue
        v="$(installed_version "$h")"
        log "    ${v:-unknown}  $h"
    done < <(find_installs)
}

if [ -n "${EAP_HOME:-}" ]; then
    if [ ! -f "$EAP_HOME/bin/standalone.sh" ]; then
        log "ERROR: EAP_HOME=$EAP_HOME has no bin/standalone.sh"
        exit 1
    fi
    HAVE="$(installed_version "$EAP_HOME")"
    if [ "${HAVE%%.*}" != "$TARGET_MAJOR" ]; then
        log "ERROR: this reproducer targets $TARGET_PRODUCT $TARGET_VERSION (from the"
        log "       support case), but EAP_HOME points at ${HAVE:-an unknown version}:"
        log "         $EAP_HOME"
        log "       Running it there would prove nothing: EAP 7 and 8 differ in EE"
        log "       namespace, so the application would not even work. Installations"
        log "       found on this machine:"
        report_candidates
        log "       Unset EAP_HOME to let this script pick the right one."
        exit 1
    fi
else
    CHOSEN=""; FALLBACK=""
    while IFS= read -r h; do
        [ -n "$h" ] || continue
        v="$(installed_version "$h")"
        [ "${v%%.*}" = "$TARGET_MAJOR" ] || continue
        case "$v" in
            "$TARGET_VERSION"*) CHOSEN="$h"; break ;;   # exact case version wins
        esac
        [ -z "$FALLBACK" ] && FALLBACK="$h"
    done < <(find_installs)
    EAP_HOME="${CHOSEN:-$FALLBACK}"
    if [ -z "$EAP_HOME" ]; then
        log "ERROR: no JBoss EAP $TARGET_MAJOR.x installation found for this reproducer"
        log "       (it targets $TARGET_PRODUCT $TARGET_VERSION). Installations found:"
        report_candidates
        log "       Set EAP_HOME explicitly, or extract the right release and set"
        log "       EAP_SEARCH_PATHS to the directory holding it."
        exit 1
    fi
    HAVE="$(installed_version "$EAP_HOME")"
    log "auto-selected $HAVE at $EAP_HOME"
fi

# Same major, different micro: allowed, but say so loudly. Version-specific
# regressions are exactly the kind that will not reproduce on the wrong CP.
case "$HAVE" in
    "$TARGET_VERSION"*) log "version matches the case exactly ($HAVE)" ;;
    *) log "WARNING: the case says $TARGET_VERSION but this install is $HAVE."
       log "         If the issue is a regression tied to a specific cumulative"
       log "         patch, it may not reproduce here. Apply the matching CP to"
       log "         be certain of a negative result." ;;
esac

if [ "${1:-}" = "--export" ]; then
    printf 'export EAP_HOME=%s\n' "$EAP_HOME"
fi
