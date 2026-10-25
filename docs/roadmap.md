# Roadmap

v2 is complete and tested end to end in QEMU (all milestones done; see [design.md](design.md) and the
decision log in §13). This lists what's left, each with a recommendation.

Never done autonomously: anything involving real disks or USB sticks, pushing to the remote, external
NixOS configuration repos, and running a migration.

## Recommended next

| Item | Recommendation | Autonomous? |
|---|---|---|
| **Try pd on real hardware** | Do it next: QEMU can't show firmware quirks, real keyboards and storage controllers, or Argon2 timing. Rehearse on a spare disk: build `setupUki` and a boot stick, run setup, install a decoy and a hidden OS, boot both, crash one (pull the power) and boot again. | No: needs real hardware. |
| **Memorable hidden offset by default** | Make `memorable_offset` the default. The offset was never a secret: someone holding the decoy can compute where the hidden boot volume would sit (just below the decoy's `tmeta`, or at D − 256 MiB on v1). What protects the hidden side is that the region is indistinguishable from the random fill without the hidden password; the offset only keeps the USB from pointing anywhere. So rounding costs nothing in security, wastes at most ~488 MiB, and makes the number easy to remember. (threat-model.md should say this plainly.) | Yes |
| **`pd rekey`** (change a password and/or the salt) | Implement. Read the side's boot volume into RAM with the old key, re-create it in place under the new key, re-open and compare before returning. A new salt means rekeying both sides and building a new USB. The risk is a crash during the rewrite (that side's `data.key` would be lost), so it keeps the contents in RAM until verified and prints a warning. | Yes |
| **Safe decoy re-initialization** | Implement a guard: `pd init-decoy` refuses if the decoy password already opens a boot volume at sector 2048, and refuses on any disk that isn't freshly randomized unless given `--force` plus either `--no-hidden` or the hidden offset and password (to check the new layout against it). Matters most for migrated v1 installs, where stock LVM would put `tmeta` on top of the v1 hidden boot volume. | Yes |
| **Event-driven driver loading** (instead of udev) | Replace `/init`'s polling loop with busybox `mdev -d` (netlink daemon, `$MODALIAS` rule → `modprobe`), keeping one `mdev -s` coldplug pass. It handles keyboards that enumerate late (USB hubs, docks) without a fixed 5-second budget, and costs nothing in size. Not full udev (see below). Testable in QEMU with a late-attached `usb-kbd`. | Yes |
| **Keymap support** | Implement if anyone needs a non-US layout. Build binary keymaps at Nix time (`loadkeys -b` from kbd) into the testenv; `pd usb` puts the configured one (`keymap = "de"`) into the per-install cpio, and `/init` loads it with busybox `loadkmap` before the password prompt. Only affects the local console. Testable by comparing `dumpkmap` output in QEMU. | Yes |

## Investigated; not recommended now

| Item | Finding | Autonomous? |
|---|---|---|
| **udev in the boot UKI** | Not worth it. systemd-udevd isn't static and would pull in glibc and systemd's libraries (~10–20 MB) plus rules; eudev is unmaintained. What it would buy is already covered: drivers load by modalias (better with `mdev -d`, above), there's no `/dev/disk/by-id` need (disks are tried in turn), no thin pool is activated (so no dmeventd), and `/dev/hidraw*` for FIDO2 comes from devtmpfs. Keymaps don't involve udev either. | — |
| **btrfs options / other filesystems** for creep without the trim | Low value now that the shutdown trim keeps footprints near live data. The churn harness can compare `nossd`, `ssd_spread`, `nodatacow`, XFS and f2fs (f2fs is log-structured and will be worst) in an afternoon of CPU time, to document which degrade least when a trim is skipped. | Yes |
| **`pd open` / `pd close` from a live ISO** | Mostly done: the setup UKI replaces the live ISO, and `pd open --rw --run` opens a side and closes it when the shell exits. Missing: read-only opening (would need a Python port of `thin-ro.sh`). Only worth it if read-only inspection turns out to be needed. | Yes |
| **FIDO2 as an extra input to Argon2** | Defer. Design: key = KDF(Argon2(password), hmac-secret response); credential ID on the USB; the hidden side derives its hmac salt from its password, so nothing hidden-specific is stored. But a token is one more thing an adversary can demand, so it may add little against the main adversary (A2), and QEMU only emulates U2F, not FIDO2 hmac-secret, so it can't be tested here. Needs your decision on the threat model and a real token. | No |
| **"Stock boot" variant** whose USB doesn't identify pd | Defer; it's a different product. The decoy would boot with stock tools (e.g. GRUB or systemd-boot with a detached-header LUKS decoy), and the hidden side would be opened by commands typed from memory in a live ISO. Most of v2 (the reversed hidden side, the read-only mapping, the monitor) carries over. A design sketch is possible autonomously; the choice of stock decoy layout is yours. | Design only |
| **Project name** | Your call. | No |
