#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
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
