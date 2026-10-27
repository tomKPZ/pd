#!/usr/bin/env bash
# Build the published outputs (the flake's `release`) and copy them, with a
# SHA256SUMS file, into DIR:
#   ci/collect-release.sh DIR      # FLAKE defaults to path:$PWD
set -euo pipefail

dir=${1:?usage: ci/collect-release.sh DIR}
FLAKE=${FLAKE:-path:$PWD}
NIX=(nix --extra-experimental-features "nix-command flakes")

out=$("${NIX[@]}" build -j auto --no-link --print-out-paths "$FLAKE#release")
mkdir -p "$dir"
for f in "$out"/*; do
    cp -L "$f" "$dir/"
    chmod 644 "$dir/${f##*/}"
done
(cd "$dir" && sha256sum -- * | grep -v ' SHA256SUMS$' >SHA256SUMS && cat SHA256SUMS)

