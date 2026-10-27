# Releases and CI

pd publishes prebuilt files, so a user doesn't have to build anything, and anyone can check that a
download is what this repository's source builds. Nothing published is specific to an install: the
files that are (`pd.toml`, FIDO2 slots, a keymap next to the boot image) are made by the user, with
`pd usb --config` ([install.md §3](install.md#3-the-boot-usb)).

## What a release contains

| File | Flake output | What it is |
|---|---|---|
| `BOOTX64.EFI` | `bootUki` | the boot image, the same for every install ([design.md §7.1](design.md#71-uki)) |
| `pd-setup.efi` | `setupUki` | the RAM-only setup environment; carries the boot image and the keymaps for `pd usb` |
| `pd.pyz` | `pdPyz` | pd as one file, for maintenance from a live system ([install.md](install.md#from-a-live-iso)) |
| `pd-boot-tools.tar.gz` | `bootToolsTarball` | the boot volume's static tools and scripts (`plainTree`), for booting from a live system |
| `SHA256SUMS` | | their SHA-256 sums |

The flake output `release` is all four, under these names: `nix build .#release`.

## Checking a download

```sh
sha256sum -c SHA256SUMS                                     # the files match the release page
gh attestation verify BOOTX64.EFI --repo tomKPZ/pd          # built by this repository's release workflow
```
The attestation (GitHub's build provenance, signed through Sigstore) ties each file to the workflow run,
commit and tag that built it. Independently of GitHub, build the same tag and compare:
```sh
git checkout vX.Y && nix build .#release && sha256sum result/*
```
Every published file is reproducible: the same commit gives the same bytes, on any x86_64 Linux machine
with Nix. CI checks this on every push (below). The pinned nixpkgs is in `flake.lock` and in the release
notes.

## Making a release

Push a tag starting with `v` (e.g. `git tag -s v2.0 && git push origin v2.0`). The release workflow
([release.yml](../.github/workflows/release.yml)):
1. builds the four files with only cache.nixos.org as a binary cache (no other cache can substitute
   anything in them), and computes `SHA256SUMS`;
2. rebuilds each of them, and the derivations they're made of, and fails if any differs
   (`ci/rebuild-check.sh`);
3. runs the unit tests and the fast QEMU subset;
4. attests their build provenance, and creates the GitHub release with notes from
   `ci/release-notes.sh`.

## CI

| Workflow | When | What |
|---|---|---|
| [ci.yml](../.github/workflows/ci.yml) | every push and pull request | build the release files and the test environment; unit tests; rebuild check; the fast QEMU subset (`test_smoke`, `test_e2e`, `test_uefi`); the release files as a workflow artifact (14 days) |
| [nightly.yml](../.github/workflows/nightly.yml) | nightly, and on demand | the whole suite, as `python3 -m unittest discover` locally (the opt-in tests that download stay manual) |
| [release.yml](../.github/workflows/release.yml) | a `v*` tag | above |

* **Runners:** GitHub's standard Linux runners (4 cores, 16 GB, KVM). `ci/enable-kvm.sh` opens
  `/dev/kvm` to the runner user; `ci/free-disk.sh` removes preinstalled toolchains so the Nix store fits.
* **Tools:** `nix develop` provides Python with pexpect and QEMU (the CanoKey build the FIDO2 tests use),
  from the same pinned nixpkgs. Locally the tests use the host's.
* **Third-party actions** are pinned to commit SHAs (with the tag in a comment): checkout,
  install-nix-action, cachix-action, upload-artifact, attest-build-provenance. Bump them deliberately.
* **Binary cache (optional):** without one, CI builds everything that isn't on cache.nixos.org from
  source: the static tools, the CanoKey QEMU, the initrds; that takes a long first run. To use
  [Cachix](https://cachix.org), create a cache, set the repository variable `CACHIX_CACHE` to its name
  and the secret `CACHIX_AUTH_TOKEN` to a token that can push. CI and the nightly run use it; the
  release workflow deliberately doesn't.
* **Failures** upload the tests' serial logs (`*.log`) as an artifact.
* **Locally:** `ci/rebuild-check.sh` and `ci/collect-release.sh DIR` run as they do in CI (with
  `path:$PWD` instead of the git flake reference). The workflows themselves are linted with
  `actionlint`.

