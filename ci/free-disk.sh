#!/usr/bin/env bash
# GitHub-hosted runners: remove preinstalled toolchains and images pd doesn't
# use, so the Nix store (kernel module closures, the setup environment, QEMU)
# and the tests' disk images fit.
set -euo pipefail

df -h / /mnt 2>/dev/null || df -h /
sudo rm -rf /usr/share/dotnet /usr/local/lib/android /opt/ghc /usr/local/.ghcup \
    /opt/hostedtoolcache /usr/local/share/boost /usr/share/swift /usr/lib/jvm \
    /usr/local/lib/node_modules /opt/microsoft /opt/google /usr/local/share/chromium \
    /usr/local/share/powershell /opt/az /usr/share/miniconda
sudo docker system prune --all --force >/dev/null 2>&1 || true
df -h / /mnt 2>/dev/null || df -h /
