#!/usr/bin/env bash
# GitHub-hosted runners: let the runner user open /dev/kvm (the QEMU tests).
set -euo pipefail

echo 'KERNEL=="kvm", GROUP="kvm", MODE="0666", OPTIONS+="static_node=kvm"' |
    sudo tee /etc/udev/rules.d/99-kvm-all-users.rules >/dev/null
sudo udevadm control --reload-rules
sudo udevadm trigger --name-match=kvm
if [ ! -w /dev/kvm ]; then
    echo "no usable /dev/kvm on this runner" >&2
    exit 1
fi

