#!/bin/bash
# Reproducer workspace: Red Hat Data Grid
#
# Drop the case into input/ and run this. Nothing to export: the workspace
# pins the product, and the package it generates refuses to run against a
# different major version even if EAP_HOME says otherwise.
WS_PRODUCT="Red Hat Data Grid"
WS_VERSION=""
WS_MAJOR="8"
WS_VERSION_HINT="8.x (version taken from input/case.txt)"
WS_LABEL="Reproducer workspace: Red Hat Data Grid"
source "$(cd "$(dirname "$0")/.." && pwd)/run-common.sh"
