#!/usr/bin/env bash
# GitHub-hosted runners: remove preinstalled toolchains pd doesn't use, so the
# Nix store (kernel module closures, the setup environment, QEMU) fits.
set -euo pipefail

df -h /
sudo rm -rf /usr/share/dotnet /usr/local/lib/android /opt/ghc /usr/local/.ghcup \
    /opt/hostedtoolcache/CodeQL /usr/local/share/boost /usr/share/swift
sudo docker image prune --all --force >/dev/null 2>&1 || true
df -h /

