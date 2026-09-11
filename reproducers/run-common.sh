#!/bin/bash
# Shared body of every workspace's run.sh. Sourced, not executed.
#
# Each workspace (eap7/ eap8/ datagrid/ jvm/) is a standing folder you drop a
# case into and run. The flow is always the same:
#
#     input/  ->  generate  ->  generated/  ->  run  ->  verdict
#
# The workspace pins the product and major version, so the eap7 folder can
# never end up running against the EAP 8 install even if EAP_HOME in your
# ~/.bashrc points there.
set -euo pipefail

WS_DIR="$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)"
AGENT_DIR="$(cd "$WS_DIR/../.." && pwd)"
INPUT="$WS_DIR/input"
OUT="$WS_DIR/generated"

REPEAT=1
GENERATE_ONLY=0
REGENERATE=0
EXTRA=()

usage() {
    cat <<USAGE
Usage: ./run.sh [options]

  --repeat N        run the reproduction N times and report a hit rate (default 1)
  --no-terminals    keep every node in this terminal (use over SSH)
  --generate-only   build the package from input/ but do not run it
  --regenerate      rebuild generated/ from scratch, discarding the old package
  -h, --help        this message

Put the case in input/:

  input/case.txt        the scenario   (REQUIRED)
  input/configs/        customer standalone-ha.xml, infinispan.xml, httpd.conf ...
  input/logs/           server.log, boot.log, gc.log, access_log ...
  input/dumps/          threaddump-*.txt, *.hprof
  input/attachments/    anything else

This workspace targets: $WS_PRODUCT $WS_VERSION_HINT
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --repeat)        REPEAT="${2:?--repeat needs a number}"; shift 2 ;;
        --no-terminals)  EXTRA+=("--no-terminals"); shift ;;
        --generate-only) GENERATE_ONLY=1; shift ;;
        --regenerate)    REGENERATE=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage; exit 1 ;;
    esac
done

# --- 1. Is there anything to reproduce? -------------------------------------
if [ ! -f "$INPUT/case.txt" ]; then
    echo "ERROR: $INPUT/case.txt does not exist."
    echo "       That file is the scenario -- logs and dumps cannot replace it."
    echo "       Start from the template:  cp ../_TEMPLATE-case.txt input/case.txt"
    exit 1
fi
if grep -q "^<What the customer reports" "$INPUT/case.txt"; then
    echo "ERROR: $INPUT/case.txt is still the unedited template."
    echo "       Fill in at least Version, Description and Actual Behavior."
    exit 1
fi

# The workspace and the case file must agree about the major version. Catching
# this here is the difference between a clear message and half an hour spent
# wondering why an EAP 8 case produced javax servlets.
CASE_VERSION="$(grep -iE '^\s*version\s*[:=]' "$INPUT/case.txt" | head -1 |
                sed -E 's/.*[:=][[:space:]]*//' | tr -d '[:space:]')"
if [ -n "${WS_MAJOR:-}" ]; then
    if [ -z "$CASE_VERSION" ]; then
        echo "ERROR: input/case.txt has no 'Version:' line."
        echo "       This workspace is for $WS_PRODUCT $WS_MAJOR.x -- state the exact"
        echo "       version, e.g. 'Version: $WS_MAJOR.4.14'. The micro version decides"
        echo "       whether a regression reproduces at all."
        exit 1
    fi
    if [ "${CASE_VERSION%%.*}" != "$WS_MAJOR" ]; then
        echo "ERROR: input/case.txt says version $CASE_VERSION, but this is the"
        echo "       $WS_PRODUCT $WS_MAJOR.x workspace."
        echo
        echo "       Use the workspace that matches the case:"
        echo "         reproducers/eap7/      JBoss EAP 7.x"
        echo "         reproducers/eap8/      JBoss EAP 8.x"
        echo "         reproducers/datagrid/  Red Hat Data Grid"
        echo "         reproducers/jvm/       JVM heap / GC / metaspace / threads"
        exit 1
    fi
    WS_VERSION="$CASE_VERSION"
fi

echo "=========================================================="
echo "  $WS_LABEL"
echo "=========================================================="
echo "  workspace : $WS_DIR"
echo "  input     : $INPUT"
for sub in configs logs dumps attachments; do
    n=$(find "$INPUT/$sub" -type f ! -name '.gitkeep' 2>/dev/null | wc -l)
    printf '    %-12s %s file(s)\n' "$sub/" "$n"
done
echo

# --- 2. Generate the package from input/ ------------------------------------
if [ "$REGENERATE" = 1 ]; then
    rm -rf "$OUT"
fi
if [ ! -d "$OUT" ] || [ "$INPUT/case.txt" -nt "$OUT/README.md" ]; then
    echo "Generating reproducer from input/ ..."
    ( cd "$AGENT_DIR" && python3 reproduce.py generate \
        -i "$INPUT" -o "$OUT" --no-openshift \
        --product "$WS_PRODUCT" ${WS_VERSION:+--version "$WS_VERSION"} )
    chmod +x "$OUT"/*.sh 2>/dev/null || true
    [ -d "$OUT/lb" ] && chmod +x "$OUT"/lb/*.sh 2>/dev/null || true
else
    echo "Reusing existing package in generated/ (--regenerate to rebuild)."
fi

if [ "$GENERATE_ONLY" = 1 ]; then
    echo
    echo "Package ready at: $OUT"
    echo "Run it with: ./run.sh --repeat 5"
    exit 0
fi

# --- 3. Run it ---------------------------------------------------------------
echo
echo "Running the reproducer (repeat=$REPEAT)..."
exec "$OUT/run-reproducer.sh" --repeat "$REPEAT" ${EXTRA[@]+"${EXTRA[@]}"}
