#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

# Script to install the development version of the Deadline Cloud for Houdini Submitter

set -euo pipefail

SCRIPT_FOLDER=$(dirname "$0")
GIT_ROOT=$(dirname "${SCRIPT_FOLDER}")
HOUDINI_INSTALL_VERSION_FILE=${GIT_ROOT}/houdini_version.txt

# Grab the Houdini version in the following priority:
#   1. CLI args, or if that doesn't exist:
#   2. houdini_version.txt
#   3. prompt the user
if [[ $# -eq 1 ]]; then
    HOUDINI_FULL_VERSION=$1
    echo "${HOUDINI_FULL_VERSION}" > "${HOUDINI_INSTALL_VERSION_FILE}"
elif [[ -f "${HOUDINI_INSTALL_VERSION_FILE}" ]]; then
    HOUDINI_FULL_VERSION=$(head -n 1 "${HOUDINI_INSTALL_VERSION_FILE}")
else
    echo "Enter the Houdini Full Version (Major.Minor.Patch): "
    read HOUDINI_FULL_VERSION
fi

# ie. 19.5.716
if [[ ! ${HOUDINI_FULL_VERSION} =~ ^[0-9]+\.[0-9]+\.[0-9]+ ]]; then
    echo "Version '${HOUDINI_FULL_VERSION}' did not Major.Minor.Patch format. Exiting"
    rm -f "${HOUDINI_INSTALL_VERSION_FILE}"
    exit 1
fi
echo "Installing Submitter for Houdini ${HOUDINI_FULL_VERSION}"

# % - delete the shortest string to the right that matches
HOUDINI_MAJOR_MINOR=${HOUDINI_FULL_VERSION%.*}
DEV_INSTALL_LOCATION=${GIT_ROOT}/dev_install
HDA_SOURCE=${GIT_ROOT}/src/deadline/houdini_submitter/otls/deadline_cloud.hda
HDA_BUILD=${DEV_INSTALL_LOCATION}/otls/deadline_cloud.hda
DEPS_BUNDLE_DEST=${DEV_INSTALL_LOCATION}/python

if [[ "${OSTYPE}" == "darwin"* ]]; then
    # macos
    HOTL_EXE=/Applications/Houdini/Houdini${HOUDINI_FULL_VERSION}/Frameworks/Houdini.framework/Versions/${HOUDINI_MAJOR_MINOR}/Resources/bin/hotl
    PREFS_FOLDER=~/Library/Preferences/houdini/${HOUDINI_MAJOR_MINOR}
    DEPS_BUNDLE_ZIP=${GIT_ROOT}/dependency_bundle/deadline_cloud_for_houdini_submitter-deps-macos.zip
elif [[ "${OSTYPE}" == "linux-gnu"* ]]; then
    # linux
    HOTL_EXE=/opt/hfs${HOUDINI_MAJOR_MINOR}/bin/hotl
    PREFS_FOLDER=~/houdini${HOUDINI_MAJOR_MINOR}
    DEPS_BUNDLE_ZIP=${GIT_ROOT}/dependency_bundle/deadline_cloud_for_houdini_submitter-deps-linux.zip
elif [[ "${OSTYPE}" == "msys" || "$OSTYPE" == "cygwin" ]]; then
    # windows via git-bash or cygwin
    HOTL_EXE="C:/Program Files/Side Effects Software/Houdini ${HOUDINI_FULL_VERSION}/bin/hotl.exe"
    PREFS_FOLDER=~/houdini${HOUDINI_MAJOR_MINOR}
    DEPS_BUNDLE_ZIP=${GIT_ROOT}/dependency_bundle/deadline_cloud_for_houdini_submitter-deps-windows.zip
else
    echo "Unknown operating system: ${OSTYPE}"
    exit 1
fi

PACKAGES_DIR=${PREFS_FOLDER}/packages

if [[ ! -f "${HOTL_EXE}" ]]; then
    echo "${HOTL_EXE} does not exist. Exiting"
    exit 1
fi

if [[ ! -f "${DEPS_BUNDLE_ZIP}" ]]; then
    echo "Grabbing Deadline Cloud for Houdini dependencies"
    "${GIT_ROOT}/depsBundle.sh"
fi

echo "Deleting existing Deadline Cloud for Houdini installation"
rm -rf "${DEV_INSTALL_LOCATION}"
rm -f "${PACKAGES_DIR}/deadline_submitter_for_houdini.json"

# ensure packages dir is created
mkdir -p "${DEV_INSTALL_LOCATION}/otls"
mkdir -p "${PACKAGES_DIR}"

echo "Creating Houdini package file at ${PACKAGES_DIR}/deadline_submitter_for_houdini.json"
# houdini env: https://www.sidefx.com/docs/houdini/basics/config_env.html
cat << EOF > "${PACKAGES_DIR}/deadline_submitter_for_houdini.json"
{
    "env": [
        {
            "PYTHONPATH": "${DEV_INSTALL_LOCATION}/python"
        }
    ],
    "hpath": "${DEV_INSTALL_LOCATION}"
}
EOF

echo "Creating Deadline Cloud HDA at ${HDA_BUILD}"
# hotl utility: https://www.sidefx.com/docs/houdini/ref/utils/hotl.html
"${HOTL_EXE}" -l "${HDA_SOURCE}" "${HDA_BUILD}"

echo "Installing Deadline Cloud for Houdini scripts"
# used to be pip install, but the installer doesn't currently pip install the package, so we just copy it over instead to match
# pip install --find-links "${GIT_ROOT}/dist/" deadline_cloud_for_houdini --no-deps --target "${DEPS_BUNDLE_DEST}"
rsync -a ${GIT_ROOT}/src/deadline/houdini_submitter/python/deadline_cloud_for_houdini "${DEPS_BUNDLE_DEST}"

echo "Unpacking Submitter Dependencies to ${DEPS_BUNDLE_DEST}"
unzip -q "${DEPS_BUNDLE_ZIP}" -d "${DEPS_BUNDLE_DEST}"

echo "Done! :)"
