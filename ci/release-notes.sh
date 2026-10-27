#!/usr/bin/env bash
# Release notes for TAG, from DIR (ci/collect-release.sh's output): what each
# file is, its SHA-256, the pinned nixpkgs, and how to check a download.
#   ci/release-notes.sh TAG DIR >notes.md
set -euo pipefail

tag=${1:?usage: ci/release-notes.sh TAG DIR}
dir=${2:?usage: ci/release-notes.sh TAG DIR}
repo=${GITHUB_REPOSITORY:-tomKPZ/pd}
docs="https://github.com/$repo/blob/$tag/docs"
nixpkgs=$(python3 -c 'import json; n = json.load(open("flake.lock"))["nodes"]["nixpkgs"]["locked"]; print(n["rev"])')

sum() { awk -v f="$1" '$2 == f { print $1 }' "$dir/SHA256SUMS"; }

cat <<EOF
Built by the release workflow from tag \`$tag\` ($(git rev-parse HEAD)), with nixpkgs \`$nixpkgs\`
(from \`flake.lock\`). Every file is reproducible: building the same commit gives the same bytes.

| File | What it is | SHA-256 |
|---|---|---|
| \`BOOTX64.EFI\` | the boot image: copy it to \`EFI/BOOT/BOOTX64.EFI\` on the boot stick ([install.md §3]($docs/install.md#3-the-boot-usb)) | \`$(sum BOOTX64.EFI)\` |
| \`pd-setup.efi\` | the RAM-only setup environment, with the boot image inside ([install.md §4]($docs/install.md#4-set-up-the-disk)) | \`$(sum pd-setup.efi)\` |
| \`pd.pyz\` | pd as one file, for maintenance from a live system ([install.md]($docs/install.md#from-a-live-iso)) | \`$(sum pd.pyz)\` |
| \`pd-boot-tools.tar.gz\` | the boot volume's static tools and scripts, for booting from a live system | \`$(sum pd-boot-tools.tar.gz)\` |

**Checking a download** ([docs/release.md]($docs/release.md)):
\`\`\`sh
sha256sum -c SHA256SUMS                                   # the files match this release
gh attestation verify BOOTX64.EFI --repo $repo            # built by this repository's release workflow
git checkout $tag && nix build .#release && sha256sum result/*   # or build it yourself and compare
\`\`\`
EOF
