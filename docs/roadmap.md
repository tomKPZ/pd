# Roadmap

v2 is complete and tested end to end in QEMU (all milestones done; see [design.md](design.md) and the
decision log in §13). This lists what's left, each with a recommendation.

Never done autonomously: anything involving real disks or USB sticks, pushing to the remote, external
NixOS configuration repos, and running a migration.

## Recommended next

| Item | Recommendation | Autonomous? |
|---|---|---|
| **Try pd on real hardware** | Do it next, following [manual-testing.md](manual-testing.md): QEMU can't show firmware quirks, real keyboards, storage controllers and NICs, or Argon2 timing. It also covers the YubiKey and stock boot items below. | No: needs real hardware. |
| **Generic boot image: the salt on the disk** | Designed in [disk-salt.md](disk-salt.md) (D55–D59). Implement it before migrating deployed v1 installs to v2: it changes the salt, so an install moved earlier would have to re-key again. The real-hardware trial can go ahead meanwhile, since little of it depends on where the salt comes from. | Yes, in QEMU. |

## Later

| Item | Finding | Autonomous? |
|---|---|---|
| **FIDO2 with a real security key** | Implemented and tested with QEMU's emulated CanoKey (design.md §4.1). Try it with a real key, in a VM (`-device usb-host,vendorid=0x1050,...`) or on the real-hardware trial. | No: needs a real key. |
| **Stock boot on real hardware** | Documented and tested in QEMU, including a hidden boot from the real Debian Live 13 image (`PD_TEST_LIVE=1`, [stock-boot.md](stock-boot.md)). Try it during the real-hardware trial, ideally also with Tails 7 (its Welcome Screen needs a GUI, so it isn't automated). | No: needs real hardware. |
| **Catch the decoy rising above the floor between hidden boots** | The frontier monitor sees only where the decoy's data is now (design.md §10.5). It could also scan the decoy's pool metadata for old btree nodes (`thin_scan`) pointing above the floor, and suggest `pd compact --hidden-offset N`. Worth it if decoys run close to the floor. | Yes. |
| **FIDO2 without a per-install file** | With the disk salt ([disk-salt.md](disk-salt.md)), `fido2.cred` is all that keeps a FIDO2 user's USB specific to the install. The slots could move to the disk, encrypted under each side's Argon2 output; the disk would then change wherever they're stored whenever a key is enrolled. | Yes. |
| **Live-system boot from the release** | The release could carry the boot tools as a static bundle that a live system downloads and runs, instead of typing stock boot's commands; with the disk salt this works for pd installs too. Needs network access at boot. | Yes. |
