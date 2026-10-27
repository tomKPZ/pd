#!/usr/bin/env bash
# GitHub-hosted runners: pick the filesystem with the most free space for the
# tests' disk images (the runner's temporary disk at /mnt is often larger than
# what's left on /), and export it as PD_TEST_DIR for the following steps.
set -euo pipefail

best=$RUNNER_TEMP
best_free=$(df --output=avail -B1 "$RUNNER_TEMP" | tail -1)
if [ -d /mnt ]; then
    free=$(df --output=avail -B1 /mnt | tail -1)
    if [ "$free" -gt "$best_free" ]; then
        sudo mkdir -p /mnt/pd-tests
        sudo chown "$(id -u):$(id -g)" /mnt/pd-tests
        best=/mnt
        best_free=$free
    fi
fi
dir=$best/pd-tests
mkdir -p "$dir"
echo "PD_TEST_DIR=$dir" >>"$GITHUB_ENV"
echo "test disk images go to $dir ($((best_free >> 30)) GiB free)"

