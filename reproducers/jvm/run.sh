#!/bin/bash
# Reproducer workspace: JVM (heap / GC / metaspace / threads)
#
# Drop the case into input/ and run this. Nothing to export: the workspace
# pins the product, and the package it generates refuses to run against a
# different major version even if EAP_HOME says otherwise.
WS_PRODUCT="JVM"
WS_VERSION=""
WS_VERSION_HINT="any JDK"
WS_LABEL="Reproducer workspace: JVM (heap / GC / metaspace / threads)"
source "$(cd "$(dirname "$0")/.." && pwd)/run-common.sh"
