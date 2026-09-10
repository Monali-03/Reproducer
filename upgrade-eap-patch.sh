#!/bin/bash
# Apply ANY EAP 7.4.x cumulative patch (CP) to a 7.4.0.GA install, so the
# reproducer runs on the customer's patched version.
#
# EAP cumulative patches are cumulative: e.g. 7.4.23 already contains every fix
# from 7.4.1 .. 7.4.22, so you apply just the one CP zip straight onto 7.4.0.GA
# (no need to apply intermediate versions first).
#
# Download the CP from the Red Hat Customer Portal (login required):
#   https://access.redhat.com/downloads/  ->  Red Hat JBoss EAP 7.4
#   file: jboss-eap-7.4.<NN>-patch.zip   (a CP/cumulative patch, NOT a full zip)
#
# Which version to test on:
#   - Match the version the CUSTOMER is running whenever you know it.
#   - If unknown, use the latest CP (e.g. 7.4.23). If the bug reproduces -> real.
#     If it does NOT reproduce on latest -> likely already fixed; advise upgrade.
#
# Usage:
#   EAP_HOME=/path/to/jboss-eap-7.4 ./upgrade-eap-patch.sh /path/to/jboss-eap-7.4.23-patch.zip
set -euo pipefail
EAP_HOME="${EAP_HOME:?Set EAP_HOME to your JBoss EAP 7.4 installation}"
PATCH_ZIP="${1:?Usage: ./upgrade-eap-patch.sh /path/to/jboss-eap-7.4.<NN>-patch.zip}"

[ -f "$PATCH_ZIP" ] || { echo "Patch zip not found: $PATCH_ZIP" >&2; exit 1; }

echo "Current patch state:"
"$EAP_HOME/bin/jboss-cli.sh" "patch info" 2>/dev/null | grep -iE "version|cumulative" || true

echo ""
echo "Applying patch: $PATCH_ZIP"
"$EAP_HOME/bin/jboss-cli.sh" "patch apply '$PATCH_ZIP'"

echo ""
echo "Patch applied. Restart EAP for it to take effect."
echo "Verify with:  $EAP_HOME/bin/jboss-cli.sh 'patch info'"
echo "The reproducer scripts already use \$EAP_HOME, so just re-run:"
echo "  ./stop-all.sh && ./start-all.sh && ./test.sh"
