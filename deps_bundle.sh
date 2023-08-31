#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
set -xeuo pipefail

python deps_bundle.py

rm -f dependency_bundle/deadline_cloud_for_houdini_submitter-deps-windows.zip
rm -f dependency_bundle/deadline_cloud_for_houdini_submitter-deps-linux.zip
rm -f dependency_bundle/deadline_cloud_for_houdini_submitter-deps-macos.zip

cp dependency_bundle/deadline_cloud_for_houdini_submitter-deps.zip dependency_bundle/deadline_cloud_for_houdini_submitter-deps-windows.zip
cp dependency_bundle/deadline_cloud_for_houdini_submitter-deps.zip dependency_bundle/deadline_cloud_for_houdini_submitter-deps-linux.zip
cp dependency_bundle/deadline_cloud_for_houdini_submitter-deps.zip dependency_bundle/deadline_cloud_for_houdini_submitter-deps-macos.zip
