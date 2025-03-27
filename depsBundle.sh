#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

# This script is called in a shell subprocess.
# If editing this script, please see the security considerations
# of this invocation method:
# https://docs.python.org/3/library/subprocess.html#security-considerations
set -xeuo pipefail

SCRIPT_FOLDER=$(dirname "$0")/scripts

pushd "${SCRIPT_FOLDER}"
python deps_bundle.py
popd

rm -f dependency_bundle/deadline_cloud_for_houdini_submitter-deps-windows.zip
rm -f dependency_bundle/deadline_cloud_for_houdini_submitter-deps-linux.zip
rm -f dependency_bundle/deadline_cloud_for_houdini_submitter-deps-macos.zip

mkdir -p dependency_bundle

cp scripts/dependency_bundle/deadline_cloud_for_houdini_submitter-deps.zip dependency_bundle/deadline_cloud_for_houdini_submitter-deps-windows.zip
cp scripts/dependency_bundle/deadline_cloud_for_houdini_submitter-deps.zip dependency_bundle/deadline_cloud_for_houdini_submitter-deps-linux.zip
cp scripts/dependency_bundle/deadline_cloud_for_houdini_submitter-deps.zip dependency_bundle/deadline_cloud_for_houdini_submitter-deps-macos.zip
