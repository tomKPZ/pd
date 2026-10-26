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
| **Catch the decoy rising above the floor between hidden boots** | The frontier monitor sees only where the decoy's data is now (design.md §10.5). It could also scan the decoy's pool metadata for old btree nodes (`thin_scan`) pointing above the floor, and suggest `pd compact --hidden-offset N`. Worth it if decoys run close to the floor. | Yes. |
| **Catch the decoy rising above the floor between hidden boots** | The frontier monitor sees only where the decoy's data is now (design.md §10.5). It could also scan the decoy's pool metadata for old btree nodes (`thin_scan`) pointing above the floor, and suggest `pd compact --hidden-offset N`. Worth it if decoys run close to the floor. | Yes. |

