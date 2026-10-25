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
| **Stock boot on a real live system** | Documented and tested in QEMU ([stock-boot.md](stock-boot.md)). Try it with a real live image (e.g. Tails 7) during the real-hardware trial: its udev, kexec, and Secure Boot off. An opt-in test could boot a downloaded Debian Live image in QEMU and type the commands. | Test: yes (downloads ~1.5 GB) |
| **Project name** | Last, once the author is satisfied. | No |
