# Roadmap

v2 is complete and tested end to end in QEMU (all milestones done; see [design.md](design.md) and the
decision log in §13). This lists what's left, each with a recommendation.

Never done autonomously: anything involving real disks or USB sticks, pushing to the remote, external
NixOS configuration repos, and running a migration.

## Recommended next

| Item | Recommendation | Autonomous? |
|---|---|---|
| **Try pd on real hardware** | Do it next: QEMU can't show firmware quirks, real keyboards and storage controllers, or Argon2 timing. Rehearse on a spare disk: build `setupUki` and a boot stick, run setup, install a decoy and a hidden OS, boot both, crash one (pull the power) and boot again. | No: needs real hardware. |

## Later

| Item | Finding | Autonomous? |
|---|---|---|
| **FIDO2 with a real security key** | Implemented and tested with QEMU's emulated CanoKey (design.md §4.1). Try it with a real key, in a VM (`-device usb-host,vendorid=0x1050,...`) or on the real-hardware trial. | No: needs a real key. |
| **Stock boot on real hardware** | Documented and tested in QEMU, including a hidden boot from the real Debian Live 13 image (`PD_TEST_LIVE=1`, [stock-boot.md](stock-boot.md)). Try it during the real-hardware trial, ideally also with Tails 7 (its Welcome Screen needs a GUI, so it isn't automated). | No: needs real hardware. |
| **In-place grow onto a bigger disk** | Today moving to a bigger disk means a fresh setup and copying each side's files with `pd open` (cloning is ruled out: two images of the same free space). A `pd grow` could do it in one step: set up the new disk with the same configuration and keys, copy each side's thin LVs (only allocated blocks, read through `thin_dump`), and keep the old disk until both sides boot. The hidden side has to move anyway, since it sits at the end of the disk. | Yes |
