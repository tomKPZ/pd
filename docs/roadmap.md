# Roadmap

v2 is complete and tested end to end in QEMU (all milestones done; see [design.md](design.md) and the
decision log in §13). This lists what's left, each with a recommendation.

Never done autonomously: anything involving real disks or USB sticks, pushing to the remote, external
NixOS configuration repos, and running a migration.

## Recommended next

| Item | Recommendation | Autonomous? |
|---|---|---|
| **Try pd on real hardware** | Do it next, following [manual-testing.md](manual-testing.md): QEMU can't show firmware quirks, real keyboards, storage controllers and NICs, or Argon2 timing. It also covers the YubiKey and stock boot items below. | No: needs real hardware. |

## Later

| Item | Finding | Autonomous? |
|---|---|---|
| **FIDO2 with a real security key** | Implemented and tested with QEMU's emulated CanoKey (design.md §4.1). Try it with a real key, in a VM (`-device usb-host,vendorid=0x1050,...`) or on the real-hardware trial. | No: needs a real key. |
| **Stock boot on real hardware** | Documented and tested in QEMU, including a hidden boot from the real Debian Live 13 image (`PD_TEST_LIVE=1`, [stock-boot.md](stock-boot.md)). Try it during the real-hardware trial, ideally also with Tails 7 (its Welcome Screen needs a GUI, so it isn't automated). | No: needs real hardware. |
| **FIDO2 without a per-install file** | With the disk salt ([design.md §4.2](design.md#42-the-salt-and-the-disks-edges)), `fido2.cred` is all that keeps a FIDO2 user's USB specific to the install. Moving the slots to the disk encrypted under each side's Argon2 output, as first proposed, doesn't work: the credential IDs would have to be stored too, and an authenticator's credential IDs can have structure (a format byte, the RP ID hash), so decrypting them with a guessed password would confirm the guess offline, the very thing FIDO2 is there to prevent. What would work: a discoverable (resident) credential on each security key, shared by both sides, so no credential ID is stored anywhere; the wrapped keys go in a few fixed slots on the disk (e.g. reserved at the end of each plain volume) and are all tried. The cost: each key then holds a credential that shows pd was used, and needs a free resident-credential slot. | Needs a decision on that trade-off. |
| **Turn on CI and cut the first release** | The workflows are written ([release.md](release.md)): build and rebuild-check the published files, unit tests and a fast QEMU subset on every push, the whole suite nightly, and a tagged release with `SHA256SUMS` and a build-provenance attestation. They're linted (`actionlint`) and their scripts run locally, but they haven't run on GitHub yet: KVM, disk space and build time on the standard runners are untested. A Cachix cache (`CACHIX_CACHE`, `CACHIX_AUTH_TOKEN`) makes runs much faster. | No: needs the repo owner (push, repository settings, a tag). |
