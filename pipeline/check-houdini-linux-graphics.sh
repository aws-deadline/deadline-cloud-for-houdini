#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
set -euo pipefail
artifacts="${1:?artifacts directory is required}"
mode="${2:?gpu or software is required}"
mkdir -p "$artifacts"
cat /etc/os-release > "$artifacts/os-release.txt"
uname -a > "$artifacts/kernel.txt"
lscpu > "$artifacts/cpu.txt"
free -h > "$artifacts/memory.txt"
{
    printf 'runner=%s\nimage=%s\nimage_version=%s\nsource=%s\n' \
        "${RUNNER_NAME:-}" "${ImageOS:-}" "${ImageVersion:-}" "${GITHUB_SHA:-}"
    if command -v nvidia-smi >/dev/null; then
        nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
    fi
    ls -l /dev/dri /usr/share/vulkan/icd.d 2>/dev/null || true
} > "$artifacts/runner.txt"
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    xvfb xserver-xorg-core xserver-xorg-video-dummy xauth x11-utils openbox xdotool \
    mesa-utils mesa-vulkan-drivers vulkan-tools dbus-x11 at-spi2-core python3-pyatspi python3-gi \
    bc libatomic1 libglu1-mesa libxkbcommon-x11-0 libxcb-cursor0 \
    libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 \
    libxcb-xinerama0 libxcb-xinput0 libopengl0 libnss3 libasound2t64
if [[ "$mode" == software ]]; then
    xvfb-run -a -s '-screen 0 1920x1080x24' bash -c \
        'LIBGL_ALWAYS_SOFTWARE=1 glxinfo -B; LIBGL_ALWAYS_SOFTWARE=1 vulkaninfo --summary' \
        > "$artifacts/graphics.txt" 2>&1
elif [[ "$mode" == gpu ]]; then
    nvidia-smi > "$artifacts/nvidia-smi.txt"
    # Validate the installed stack. Driver installation requires separate approval.
    command -v nvidia-xconfig > "$artifacts/nvidia-xconfig.txt"
    ldconfig -p | grep -E 'libGLX_nvidia|libnvidia-gl|libvulkan' > "$artifacts/graphics-libraries.txt"
else
    echo "Unknown graphics mode" >&2
    exit 2
fi
