#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# Microsoft-supported Azure T4 GRID package:
# https://learn.microsoft.com/en-us/azure/virtual-machines/linux/n-series-driver-setup
set -euo pipefail
artifacts="${1:?artifacts directory is required}"
mkdir -p "$artifacts"
if command -v nvidia-smi >/dev/null && nvidia-smi > "$artifacts/nvidia-smi.txt"; then
    command -v nvidia-xconfig > "$artifacts/nvidia-xconfig.txt"
    exit 0
fi
# This helper is only for a disposable Azure T4 runner, before its desktop starts.
vm_size="$(curl --fail --silent --show-error --noproxy '*' \
    -H Metadata:true 'http://169.254.169.254/metadata/instance/compute/vmSize?api-version=2021-02-01&format=text')"
[[ "$vm_size" =~ ^Standard_NC[0-9]+as_T4_v3$ ]] || {
    echo 'Driver installation requires a supported Azure NCasT4_v3 runner.' >&2
    exit 1
}
if lsmod | grep -q '^nouveau '; then
    echo 'Nouveau is loaded; image preparation and a reboot are required.' >&2
    exit 1
fi
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    build-essential "linux-headers-$(uname -r)" pkg-config libglvnd-dev
directory="$(mktemp -d)"
trap 'rm -rf "$directory"' EXIT
installer="$directory/nvidia-grid.run"
curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
    https://download.microsoft.com/download/4efc1974-7b6f-431f-ab2d-b8d99bb8a512/NVIDIA-Linux-x86_64-580.178.04-grid-azure.run \
    --output "$installer"
echo "100dab7559cfa1cf896fac923681ba93fe75c1b5ac7102f9264396a2585d67af  $installer" | sha256sum --check
sudo sh "$installer" --silent --no-dkms > "$artifacts/driver-install.txt" 2>&1 || {
    sudo cp /var/log/nvidia-installer.log "$artifacts/nvidia-installer.log"
    sudo chown "$(id -u):$(id -g)" "$artifacts/nvidia-installer.log"
    echo 'GRID installation failed; inspect the installer artifact.' >&2
    exit 1
}
nvidia-smi > "$artifacts/nvidia-smi.txt"
command -v nvidia-xconfig > "$artifacts/nvidia-xconfig.txt"
