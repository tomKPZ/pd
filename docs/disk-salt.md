# The salt on the disk: a generic boot image (planned)

**Status:** agreed, not implemented ([design.md](design.md) D55–D59). When it lands, the documents
listed in §12 change and this text moves into design.md.

## 1. Motivation

Today the boot USB belongs to one install: its UKI carries `salt.txt` (design.md §4, §7.1). So:
* **The USB is the salt's only copy besides `pd.toml`,** which install.md says to wipe from the setup
  stick. Losing both loses both sides. Rebuilding the USB, even for a kernel update, needs one of them
  (`pd usb --salt-from OLD.EFI`).
* **The USB proves an install was made.** A UKI with a unique salt shows that someone ran pd's setup,
  not just that they downloaded pd.
* **The disk has a fixed pattern:** zeros in the first and last MiB, random data in between.

After the change:
* the boot image is the same file for every install, built reproducibly by Nix and published with its
  hash, so it can be downloaded, checked against the hash, and replaced;
* the salt lives on the disk, next to the data it protects;
* every byte of the disk looks like the random fill.

**What it doesn't change:** someone with the decoy password still sees a pd decoy. Not looking like pd
at all is what [stock boot](stock-boot.md) is for.

## 2. The salt

* **The salt is the 32 bytes just below 1 MiB, `[1 MiB − 32, 1 MiB)`,** as 64 lowercase hex characters
  (the string passed to Argon2, like today's salts). Both sides use it.
* **Read back, not chosen.** `pd randomize` fills these bytes along with the rest of the disk, and setup
  reads them back.
* **Why the end of the first MiB.** Tools that write to the front of a disk leave it alone: an MBR (512
  bytes), a primary GPT (17 KiB with 512-byte sectors, 24 KiB with 4 KiB sectors), Windows' "Initialize
  disk". Partitions conventionally start at 1 MiB, and partitioning tools don't write the gap below.
* **Why only 32 bytes.** Hashing a larger range adds nothing: any changed byte changes the salt, so a
  smaller range is exposed to less.
* **No redundancy.** Copies or error correction that anyone can decode would also let anyone check that
  they agree, which random data never does. A single range of random bytes is indistinguishable from
  the fill.
* **Typed in a live system:** `S=$(od -An -tx1 -v -j 1048544 -N 32 /dev/nvme0n1 | tr -d ' \n')`.

## 3. The disk's edges

The first and last MiB keep their jobs (design.md §3: room for a partition table, and for the backup GPT
that a tool which "initializes" the disk writes), but hold random data instead of zeros.

* **`pd randomize` fills the whole disk,** edges included, through the throwaway-key mapping
  (`zero_header` goes away).
* **Setup then checks both edges,** and refills one with fresh random data if it looks like anything:
  * the boot's own check (§4), run from the same busybox function, so a disk that setup accepts is
    never put last at boot;
  * `wipefs`, i.e. libblkid's superblock and partition-table probes, including end-of-disk ones such as
    a backup GPT or RAID metadata;
  * the setup kernel's partition parsers, after re-reading the partition table. The setup kernel enables
    all of them, so a weak parser that some distro kernel has (Atari, Amiga, …) can't find phantom
    partitions later.
* **A refill happens about once in 65,536 disks,** almost always for the 2-byte MBR signature. Rejecting
  those shifts the distribution by about that much, which nothing can observe. A disk wiped with random
  data shows the signature that rarely too.
* **Nothing is kept to prove the edges are filler** (D59): no seed, no fill key. The free space can never
  be proven unused, so proving only the header would invite the question why. Keeping the fill key would
  map every block the hidden side wrote (design.md §2.4).

## 4. Boot

early.sh no longer derives one key for every disk: each candidate disk has its own salt.

1. Read the password. An empty one still goes to the recovery shell, and the hidden boot's `sed` is
   unchanged.
2. Sort the disks in `/sys/block`:

   | Disk | Treatment |
   |---|---|
   | loop, ram, zram, dm, sr; smaller than 2 GiB (setup's minimum) | never tried |
   | salt bytes all the same value (zeros on a new or trimmed SSD, `0xFF` on erased flash) | never tried: 32 random bytes are never all equal, and a disk whose salt was overwritten can't boot anyway |
   | looks partitioned (below) | tried last |
   | everything else | tried first |

   Largest first within each group. A disk looks partitioned if the kernel created partitions for it,
   busybox `blkid` names a type, bytes 510–511 are `55 AA`, or `EFI PART` sits at byte 512 or 4096. The
   boot USB itself is never tried (the bytes just below its ESP at 1 MiB are zeros) or tried last.
3. For each candidate in order: read its salt, run Argon2 (reusing the key if an earlier disk had the
   same salt), unwrap the FIDO2 slots if `fido2.cred` exists, and try the keys as today. Stop at the
   first disk that opens.
4. If none opens: "No bootable disk found", and the recovery shell.

* **Cost:** one Argon2 run (about 0.3 s) per disk tried, plus one security-key touch with FIDO2. With the
  right password on a machine with one pd disk, that's one, as today. Each wrong password costs one per
  candidate. Partitioned-looking disks are tried last rather than skipped: a pd disk whose front sectors
  something overwrote still boots, and only wrong passwords pay for it.
* **The password stays in memory** until a disk opens or all have failed, instead of being dropped after
  one Argon2 run. `exec late.sh` and `init_on_free=1` still clear it.
* **FIDO2:** the PIN is asked once. The security key's input derives from the per-disk Argon2 output
  (design.md §4.1), so each disk tried needs its own touch.
* **The recovery shell can name the disk,** for machines with several random-looking disks:
  `PD_DISK=/dev/nvme0n1 /early.sh`.
* **After kexec nothing changes.** The target's initrd hook already finds the disk by trying the data key
  on every disk (`target/hook.sh`), without udev or Argon2.

## 5. What stays on the USB

* **`EFI/BOOT/BOOTX64.EFI`, the release image:** kernel, initrd, `early.sh`, and an `early_env.sh` with
  the offsets, the cipher, and fixed Argon2 parameters (today's defaults: t=1, m=1 GiB, p=4). Changing
  the defaults later is a format change: installs would have to re-key.
* **Optional files in `EFI/BOOT/BOOTX64.EFI.extra.d/`.** systemd-stub packs the `*.cred` files it finds
  there into a cpio under `/.extra/credentials/` in the initrd, so the image itself never changes:

  | File | Contents | Reveals |
  |---|---|---|
  | `keymap.cred` | the console keymap (`.bmap`) | the user's keyboard layout |
  | `fido2.cred` | `fido2.txt` | a FIDO2 enrollment, so an install |
  | `argon2.cred` | other Argon2 parameters (the tests use this) | an install with custom parameters |
  | `salt.cred` | an explicit salt, for installs that haven't moved to the disk salt | an install |

  A USB without FIDO2, custom parameters or an explicit salt is the release file plus, at most, a keymap.

## 6. The release image

* **Built by Nix,** not assembled by `pd usb` at USB time: ukify with the boot kernel, the boot initrd,
  the command line, and the neutral os-release `pd usb` already passes. The setup UKI is already generic.
* **Reproducible.** `nix build --rebuild` checks locally that a rebuild matches bit for bit. A release
  publishes `BOOTX64.EFI`, `setup.efi` and their SHA-256 sums, so anyone can compare a downloaded or
  self-built image with the published hash.
* **Unsigned,** as now (design.md §12: Secure Boot isn't planned).

## 7. Commands

* **`pd usb`** writes the release image and any optional files to a stick. Without optional files it
  needs no config.
* **`salt` in the config becomes optional:** absent means the disk salt, which `pd open`, `rekey`,
  `compact` and `fido2` read from `disk`. `pd salt` goes away.
* **`pd rekey --new-salt` becomes `--new-header`:** a new random first MiB, and both sides re-keyed. The
  old salt stays in a file on the setup stick until both sides are done, so an interrupted run can be
  finished. The USB doesn't change.
* **`pd move`** gives the new disk its own edges and salt, and keys both boot volumes there with it (it
  already asks for both passwords). The USB doesn't change.
* **`pd migrate-v1`** writes the header and uses its salt as the new one.
* **A pd install can also boot from a live system** with stock boot's typed commands, with the salt read
  by the `od` line in §2. Without the USB, the install is no longer locked out.
* **Stock boot itself is unchanged:** its first MiB is a real partition table, and the salt stays the
  decoy's LUKS UUID or a typed value.

## 8. Security

* **Offline guessing no longer needs the USB.** Today, someone with only the disk can't start guessing,
  because the salt is on the USB. Afterwards the disk alone is enough. The password carries the security
  either way (whoever holds both the USB and the disk can already guess), and FIDO2 is the way to add a
  second factor. The threat model has to say so.
* **The USB proves less:** a public file whose hash matches the release, not a record that an install
  was made (except for the optional files that reveal one, §5).
* **The disk has no fixed pattern.**
* **Nothing changes for someone with the decoy password.** The decoy uses the salt itself, and the header
  is the same for both sides (invariant 1).
* **The header only changes with `--new-header`,** so snapshots of the disk don't see it change.

## 9. Migration

For a v2 install, or a migrated v1 install (whose first MiB is also v1's zero header), with both
passwords (or `--no-hidden`) and the old salt (from the config or the old USB):
1. Check that the first MiB is all zeros, so nothing uses it. On a migrated v1 layout, also check that
   the hidden side's lowest allocated block is well above 256 MiB: v1's hidden table nominally reaches
   down to 0.
2. Write a random first MiB, checked as in §3. Fill the last MiB the same way (zeros on v2 layouts,
   already random on v1 layouts).
3. Re-key each side from the old salt to the disk salt, with `pd rekey`'s existing code. After an
   interruption, run the migration again: each side opens with exactly one of the two salts, and the old
   one is still in the config.
4. Check that both sides open, then write the release image to the USB.

As with any `pd rekey`, an interruption *while* a boot volume is being rewritten loses that boot volume,
since its only complete copy is in RAM. That limitation isn't new.

## 10. Alternatives considered

| Alternative | Verdict |
|---|---|
| Salt on the USB (today) | One Argon2 run tries every disk, but the USB belongs to one install and holds the salt's only copy. |
| Salt = hash of the whole first MiB | Any changed byte destroys it; nothing gained over 32 bytes. |
| Redundant, recoverable salt | Redundancy that anyone can decode, anyone can check: the first MiB would no longer look random. |
| First MiB generated from a stored seed, to prove it hides nothing | The free space can't be proven unused anyway (D59). |
| Skip empty disks by a zero first sector | Weaker than checking the salt bytes: misses disks that are zero only at the salt, and skips a pd disk whose first sector was overwritten. |
| Skip partitioned-looking disks entirely | A pd disk whose front sectors were overwritten would never boot. Trying them last costs only on wrong passwords. |
| Ask for the disk at boot | Not needed: the order finds it, and after kexec the hook scans with the data key anyway. The recovery shell can still name one. |
| One FIDO2 touch for all disks (the security key's input derived from the password alone) | Changes the FIDO2 format and adds an Argon2 run, while one touch per candidate rarely means more than one. Later, if needed. |
| Keep the zero edges | They're a fixed pattern, and the buffers don't need zeros. |

## 11. Tests

* Unit tests: salt read-back; the edge check rejects `55 AA`, `EFI PART`, a filesystem superblock and an
  all-equal salt; candidate order.
* `test_e2e` with the release image: both sides boot. An extra empty disk is never tried, and an extra
  partitioned disk is tried last (one Argon2 run with the right password, visible in the log).
* A pd disk with a GPT written over its front still boots.
* `pd randomize`: no zeros at the edges, `wipefs` finds nothing, and the salt early.sh reads equals
  setup's.
* `--new-header`, `pd move`, and migration from today's layout and from a migrated v1 disk: both sides
  boot with the same USB.
* `test_fido2`: one touch with one candidate disk.
* `test_uefi`: the release image from a USB stick under OVMF, with a keymap file in
  `BOOTX64.EFI.extra.d/`.
* The tests' fast Argon2 parameters come from an `argon2.cred` file.

## 12. Documents to update when it lands

* design.md §2.4, §3, §4, §4.1, §7.1, §7.2, §9, §12;
* threat-model.md: offline guessing, and the table of things that could give you away;
* install.md §3 (the boot USB) and §4;
* booting.md (rebuilding the USB), stock-boot.md, migration.md, manual-testing.md.
