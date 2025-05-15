#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

# This script is called in a shell subprocess.
# If editing this script, please see the security considerations
# of this invocation method:
# https://docs.python.org/3/library/subprocess.html#security-considerations
set -xeuo pipefail

python3 scripts/deps_bundle.py