#!/usr/bin/env bash
# Rebuild every published output, and the derivations it's assembled from,
# and fail if any rebuild differs bit for bit from the first build
# (`nix build --rebuild`).  Runs in CI and locally:
#   ci/rebuild-check.sh            # FLAKE defaults to path:$PWD
set -euo pipefail

FLAKE=${FLAKE:-path:$PWD}
NIX=(nix --extra-experimental-features "nix-command flakes")
# The published files (release), then what they're made of: a rebuild of a
# top-level derivation doesn't rebuild its inputs.
ATTRS=(bootUki setupUki pdPyz liveTarball bootInitrd setupInitrd bootTools plainTree)

# Evaluate once, so that the build and the rebuilds use the same derivations
# even if the working tree changes meanwhile.
drvs=()
for attr in "${ATTRS[@]}"; do
    drvs+=("$("${NIX[@]}" eval --raw "$FLAKE#$attr.drvPath")^*")
done
"${NIX[@]}" build -j auto --no-link "${drvs[@]}"
for i in "${!ATTRS[@]}"; do
    echo "rebuilding ${ATTRS[$i]}"
    "${NIX[@]}" build -j auto --no-link --rebuild "${drvs[$i]}"
done
echo "all ${#ATTRS[@]} outputs rebuild bit for bit"

