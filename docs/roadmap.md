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
| **btrfs options / other filesystems** for creep without the trim | Low value now that the shutdown trim keeps footprints near live data. The churn harness can compare `nossd`, `ssd_spread`, `nodatacow`, XFS and f2fs (f2fs is log-structured and will be worst) in an afternoon of CPU time, to document which degrade least when a trim is skipped. | Yes |
| **`pd open` / `pd close` from a live ISO** | Mostly done: the setup UKI replaces the live ISO, and `pd open --rw --run` opens a side and closes it when the shell exits. Missing: read-only opening (would need a Python port of `thin-ro.sh`). Only worth it if read-only inspection turns out to be needed. | Yes |
| **FIDO2 as an extra input to Argon2** | Wanted eventually. Design: key = KDF(Argon2(password), hmac-secret response); credential ID on the USB; the hidden side derives its hmac salt from its password, so nothing hidden-specific is stored. But a token is one more thing an adversary can demand, so it may add little against the main adversary (A2), and QEMU only emulates U2F, not FIDO2 hmac-secret, so it can't be tested here. Needs a decision on the threat model and a real token. | No |
| **"Stock boot" variant** whose USB doesn't identify pd, plus a guide | Later task, e.g. the boot stick is a stock Debian Live or Tails image. The decoy would boot with stock tools (e.g. GRUB or systemd-boot with a detached-header LUKS decoy), and the hidden side would be opened by commands typed from memory in a live ISO. Most of v2 (the reversed hidden side, the read-only mapping, the monitor) carries over. A design sketch is possible autonomously; the choice of stock decoy layout is the author's. | Design only |
| **Project name** | Last, once the author is satisfied. | No |
