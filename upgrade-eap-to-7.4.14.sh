#!/bin/bash
# Apply the EAP 7.4.14 cumulative patch to a 7.4.0.GA install, so the session
# reproducer runs on the customer's regressed version.
#
# You must first download the patch from the Red Hat Customer Portal (login req'd):
#   https://access.redhat.com/downloads/  ->  Red Hat JBoss EAP 7.4
#   file: jboss-eap-7.4.14-patch.zip   (a CP/cumulative patch, NOT a full zip)
#
# Usage:
#   EAP_HOME=/path/to/jboss-eap-7.4 ./upgrade-eap-to-7.4.14.sh /path/to/jboss-eap-7.4.14-patch.zip
set -euo pipefail
EAP_HOME="${EAP_HOME:?Set EAP_HOME to your JBoss EAP 7.4 installation}"
PATCH_ZIP="${1:?Usage: ./upgrade-eap-to-7.4.14.sh /path/to/jboss-eap-7.4.14-patch.zip}"

echo "Current version:"
grep -h "Version" "$EAP_HOME/version.txt" 2>/dev/null || true

echo "Applying patch: $PATCH_ZIP"
"$EAP_HOME/bin/jboss-cli.sh" "patch apply '$PATCH_ZIP'"

echo ""
echo "Patch applied. Restart EAP for it to take effect."
echo "Verify with:  $EAP_HOME/bin/jboss-cli.sh 'patch info'"
echo "The session reproducer scripts already use \$EAP_HOME, so just re-run:"
echo "  ./stop-all.sh && ./start-all.sh && ./test.sh"
