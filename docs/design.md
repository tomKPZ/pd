# Plain Disguise (pd) design (v2)

**Status.** This is the v2 design, implemented on branch `v2` and covered by the QEMU test suite
(§11). Where v1 differs, the text says so; the complete as-built description of v1 is in
[v1.md](v1.md). Statements marked *(verify)* describe behavior the test suite doesn't cover yet.

**Contents**
1. [Goals](#1-goals)
2. [The core trick: two thin pools growing toward each other](#2-the-core-trick-two-thin-pools-growing-toward-each-other)
3. [Disk layout](#3-disk-layout)
4. [Keys and encryption](#4-keys-and-encryption)
5. [The plain volume](#5-the-plain-volume)
6. [The data region](#6-the-data-region)
7. [Boot chain](#7-boot-chain)
8. [Multiple systems per side](#8-multiple-systems-per-side)
9. [Setup](#9-setup)
10. [Space management and safety](#10-space-management-and-safety)
11. [Build and test](#11-build-and-test)
12. [Alternatives considered](#12-alternatives-considered)
13. [Decision log](#13-decision-log)
14. [Open questions](#14-open-questions)

---

## 1. Goals

**The golden rule:** the decoy looks normal and never sees hidden data. The two systems are separated
by a whole OS boundary: separate kernels, root filesystems, home directories, logs, and caches.
Nothing the decoy can read refers to the hidden system. That includes the decoy's disk contents, the
boot USB, and NVRAM.

Requirements:
* **No unencrypted data on disk.** Someone who finds the disk sees random data from the first byte to
  the last, and no partition table: a drive that looks wiped.
* **The first MiB holds nothing but random data and, at its end, the salt** (§4.2). The decoy's plain
  volume then starts at sector 2048, the standard first-partition offset. 2048 is the one number the
  hidden boot replaces, with a single hand-typed `sed`.
* **The boot USB is the same for everyone:** a downloadable, reproducible image (§7.1), plus optional
  files next to it.
* **NVRAM is cleaned on every boot.** The firmware's boot entry for the USB is deleted. A machine
  found without its USB shows no sign of having booted from one.
* **One password prompt per boot.** Keys pass from stage to stage in RAM by being appended to the next
  initrd, so nothing is typed twice.
* **Keys never touch persistent storage.** They live in tmpfs and are shredded after use.
* **Stock kernel features only.** No custom modules.
* **The adversary may hold the boot USB and the decoy password.**
* **Uniform boot.** Decoy and hidden systems boot through the same code path.

Nice to have:
* Changing the password without re-encrypting the disk (v2 gets this from `data.key`, §4).

Non-goals:
* **Hiding that the USB is a pd boot USB.** That is no longer possible, because the repository is
  public ([threat-model.md](threat-model.md)). A variant that avoids this is possible but costs a lot of
  typing: a stock decoy boot (e.g. GRUB with a detached LUKS header), with the hidden system booted from
  a live ISO by memorized commands (argon2, cryptsetup, mount).
* **Secure Boot.** It ties the disk to one machine. It could be supported through the whole boot chain,
  but isn't planned.

Out of scope: snapshot adversaries, flash wear-leveling forensics, RAM attacks, and evil-maid attacks.
See [threat-model.md](threat-model.md).

---

## 2. The core trick: two thin pools growing toward each other

### 2.1 dm-thin allocates sequentially
dm-thin hands out free data blocks next-fit, in `sm_disk_new_block()` in
`drivers/md/persistent-data/dm-space-map-disk.c`:
* The search starts at a cursor `begin` and runs to the end of the data device.
* It wraps to 0 only when it reaches the end.
* After each allocation, the cursor moves past the allocated block.
* The cursor resets to 0 when the pool is opened (`dm_sm_disk_open()`), i.e. on every activation.

So a fresh pool fills its data device from the start.

This is an implementation detail, not a documented interface, and the whole scheme depends on it. The
test suite therefore checks it for every kernel pd ships. The target OSes' kernels must keep this
behavior too.

### 2.2 Reversal
The hidden pool's PV is not the disk itself but a dm-linear device. That device maps the hidden data
region in reverse order of 256 MiB chunks: logical chunk 0 is the physically last chunk. The hidden
pool's sequential allocation therefore moves from the end of the disk toward the start.

Choosing the chunk size is a tradeoff:
* Smaller chunks give a sharper boundary between the two sides, with less interleaved slack where they
  meet.
* Larger chunks keep the table shorter. At 256 MiB, a 1 TB disk needs about 4k lines.

256 MiB was verified on a 1 TB drive to cost no measurable performance. Data inside a chunk still runs
forward, so a mechanical disk doesn't thrash.

Reversal has to happen *below* LVM. Doing it inside LVM, by allocating the hidden pool's extents in
descending order, doesn't work: the PV label and LVM metadata sit at the low end of the PV. That would
put them at the collision frontier, where they would be destroyed first.

### 2.3 Discards: never to the disk, and to the pool only at shutdown
This is the central safety invariant. There are three layers:
* **dm-crypt is opened without `allow_discards`.** No discard ever reaches the disk. This is the
  deniability barrier: a discarded block reads back as zeros or is tracked by the SSD, which would
  reveal which blocks hold data.
* **Filesystems are mounted `nodiscard`,** and nothing trims while the pool is active (no `fstrim`
  timer, no `discard` mount option).
* **Thin pools use `--discards=nopassdown`,** and each OS runs `fstrim` once, at shutdown
  (`pd.trimOnShutdown` in the NixOS module, `target/arch/fstrim-before-umount.service` on Arch). The
  pool unmaps what the filesystems freed; nothing passes down to dm-crypt.

**Why not discard continuously.** This part is counterintuitive. dm-thin's cursor is next-fit and
cyclic, and it's reset to 0 only when the pool is activated. Take a pool of 8 blocks
(A = allocated, F = free) and a filesystem that keeps allocating and freeing one block, with discards on:
```
start:             AAAFFFFF
allocate a block:  AAAAFFFF
free block 3:      AAFAFFFF   (the discard unmaps it in the pool)
allocate again:    AAFAAFFF   (the cursor has moved on; the hole isn't reused)
```
Churning a single block walks the cursor to the end of the pool, and from there into the other side's
data. Without discards, the pool never learns that a block was freed. The filesystem reuses its own
freed blocks, which are already mapped, so nothing new is allocated.

**Why trim at shutdown.** Without any discards, a side's physical footprint is the number of distinct
virtual chunks its filesystems have ever written: a high-water mark of the filesystems' *address*
footprint, which only grows. Trimming once at shutdown unmaps everything the filesystems have freed;
at the next boot the pool is activated again, the cursor starts at 0, and new allocations fill the
holes from the front. The footprint then stays near the live data plus one session's new writes. A trim
in the *middle* of a session is harmful: it frees holes behind the cursor, and new writes go to fresh
space beyond it.

Measured with `tests/fs_survey.py` (the same harness as `tests/test_churn.py`): an 8 GiB thin
volume, 60 cycles, about 2 GiB of live data. *churn* fills to 25% with mixed file sizes (4 KiB to
32 MiB) and deletes half at random each cycle. *rewrite* fills to 25% with 256 MiB files and then
overwrites random 1 MiB ranges in place, about 1 GiB per cycle (VM images, databases). "Trim" means
`fstrim` plus pool reactivation every 5 cycles, i.e. a shutdown. Each cell is the high-water mark
(the highest allocated pool block, which is what decides a collision) at the end, in GiB, with the
filesystem's own peak usage in parentheses:

| Filesystem (mount options) | churn, no trim | churn, trim | rewrite, no trim | rewrite, trim |
|---|---|---|---|---|
| btrfs (`ssd`, the default on SSDs) | 3.1 (2.1) | 2.6 (2.1) | 6.4 (4.5) | 5.6 (4.4) |
| btrfs `nossd` | 3.2 (2.1) | 2.6 (2.1) | 6.5 (4.4) | 5.6 (4.4) |
| btrfs `ssd_spread` | 3.6 (2.1) | 3.0 (2.1) | 6.6 (4.4) | 5.7 (4.3) |
| btrfs `autodefrag` | 3.2 (2.1) | 2.6 (2.1) | 6.5 (4.4) | 5.5 (4.3) |
| btrfs `compress-force=zstd` | 2.9 (2.1) | 2.6 (2.1) | 3.8 (2.0) | 3.2 (2.0) |
| btrfs `nodatacow` | 3.1 (2.1) | 2.6 (2.1) | 2.0 (2.0) | 2.0 (2.0) |
| ext4 | 7.6 (2.0) | 4.9 (2.0) | 2.1 (2.0) | 2.2 (2.0) |
| XFS | 3.5 (2.0) | 2.8 (2.0) | 2.1 (2.2) | 2.1 (2.2) |
| f2fs | 2.0 (2.0) | 1.9 (2.0) | 2.0 (2.2) | 2.0 (2.2) |

Over 200 churn cycles without a trim, btrfs stays at 3.2 GiB (flat after the first few cycles), f2fs
at 2.0 GiB, and XFS creeps slowly from 3.5 to 3.8 GiB. (Earlier runs reported btrfs creeping to
6.5 GiB; that was the harness overshooting its fill target, because btrfs accounts space lazily.)

**Chunk size doesn't matter.** These runs use 64 KiB pool chunks, which LVM picks for small disks; it
picks 512 KiB for 1 TiB, 2 MiB for 4 TiB and 4 MiB for 8 TiB (keeping the pool metadata at 128 MiB,
twice what a full pool needs), and the chunk size is fixed when the pool is created. Rerun with 512 KiB
and 2 MiB chunks, btrfs and XFS footprints stay within a few percent of the 64 KiB numbers in every
column.

* **btrfs** allocates data block groups first-fit from low device offsets and deletes empty block
  groups; with the shutdown trim its footprint tracks live data. Root volumes are btrfs (the boot
  path mounts their `@boot` and `@nix` subvolumes), and so are extra volumes by default. Its weak spot is
  rewriting parts of large files: copy-on-write writes the new data elsewhere, and an extent stays
  allocated until none of it is referenced (extent "bookending"), so the filesystem itself grows to
  more than twice its live data. `compress-force` caps extents at 128 KiB, which mostly fixes this at
  some CPU cost; `nodatacow`, or `chattr +C` on the directories holding such files, fixes it entirely,
  at the cost of checksums and compression for those files. `ssd` vs `nossd` doesn't matter;
  `ssd_spread` is slightly worse; `autodefrag` doesn't help.
* **XFS** is the alternative for extra volumes (`fs = "xfs"` in `[[volumes]]`). In-place rewrites reuse
  mapped blocks, so it suits large files rewritten in place, and under churn it stays moderate even
  without a trim (creeping slowly: 3.8 GiB after 200 cycles). But XFS can't shrink, so `pd move` to a
  smaller disk leaves an XFS volume at its old size (§10.4); btrfs shrinks, which is why it's the
  default. For files rewritten in place on btrfs, use `chattr +C` directories.
* **ext4** isn't supported. It deliberately spreads allocations and metadata across block groups (the
  Orlov allocator): with churn and no trim it reaches the whole volume, and even with the shutdown
  trim a single session's writes spread it out (the ~5 GiB comes from writing about 5 GiB in one
  session on an 8 GiB volume). XFS is as good for rewrites and better everywhere else.
* **f2fs** has the smallest footprint in every column. Although it's log-structured, it reuses freed
  segments instead of advancing through fresh ones. This was measured on an 8 GiB volume only;
  f2fs's allocation policy (switching between appending and reusing segments) depends on free space,
  so it isn't established for large volumes, and its tooling is less mature than the others'.
* **Without the shutdown trim** (or if it's skipped, e.g. after a crash), footprints are as in the
  "no trim" columns: about 1.5× live data for btrfs and 1.7× for XFS under churn. The
  hidden side's thin LVs are capped (§6.2), so it can't grow past its cap either way;
  the decoy is bounded only by the disk, and the frontier monitor (§10.2) warns as the gap shrinks.
* **Thin snapshots** break these bounds, because copy-on-write allocates new chunks for shared data.
  btrfs snapshots are fine. `btrfs balance` allocates new block groups and raises the high-water mark
  for the rest of the session.

For users: `df` doesn't show how much of the disk a side occupies. The pool's `data_percent` (from
`lvs`) does.

### 2.4 Randomized disk
Setup first fills the whole disk with random data. It opens a throwaway plain dm-crypt mapping over the
entire disk and writes zeros through it. cryptsetup reads that mapping's key from `/dev/urandom` itself,
so the key never enters userspace. Everything either side writes later is ciphertext under other keys,
which is indistinguishable from the fill. The fill checks that every byte was written: a short fill
would leave old contents where hidden data later stands out.

(v1 generated this key in Python and printed it at the end, for debugging. Anyone holding that key
could map out every block written since setup.)

---

## 3. Disk layout

`D` is the disk size rounded down to 1 MiB. All offsets are physical.

```
0      1 MiB         256 MiB                                                  H-255 MiB      H             D-1 MiB    D
|header| decoy plain | PV hdr, swap, decoy thin data →     ...     ← hidden thin data | hidden plain | decoy tmeta |trailer|
                     |<------------------------ decoy data region [256 MiB, D-1 MiB) --------------------------->|
                     |<--------- hidden data region [256 MiB, H-255 MiB), reversed --------->|
```

| Region | Range | Contents |
|---|---|---|
| header | [0, 1 MiB) | random; its last 32 bytes are the salt (§4.2) |
| decoy plain volume | [1 MiB, 256 MiB) | plain dm-crypt (Argon2 key), btrfs |
| decoy data region | [256 MiB, D − 1 MiB) | plain dm-crypt (decoy `data.key`), LVM PV |
| hidden data region | [256 MiB, H − 255 MiB), reversed in 256 MiB chunks | plain dm-crypt (hidden `data.key`), LVM PV |
| hidden plain volume | [H − 255 MiB, H) | plain dm-crypt (Argon2 key), btrfs |
| trailer | [D − 1 MiB, D) | random, unused |

* **The decoy data region spans the rest of the disk**, because an ordinary install would. The hidden
  regions lie inside it, in space the decoy's pool hasn't allocated.
* **The first and last MiB are random data, never used for data** (§4.2). The first leaves room for a
  partition table and holds the salt. The last is where a tool that "initializes" the disk (Windows
  Disk Management offers to; gdisk's repairs do it) writes a backup GPT. Without the trailer that would
  land on the decoy pool's `tmeta`, and a decoy that stops working is suspicious in itself. (Until D56
  both were zeros, a fixed pattern.)
* **What H is.** The decoy's LVM layout is stock (§6.1). Measured with LVM 2.03.41, stock placement
  is `[swap][pmspare][pool data][tmeta]`: the spare metadata LV goes in front of the pool's data and
  `tmeta` goes at the very end of the PV. The decoy writes to `tmeta`, so the hidden side must stay below
  it. **H** is the physical start of the decoy metadata that lies after the pool's data. Setup reads it
  back from the decoy's LVM metadata after creating the decoy, rather than assuming it. If there's none
  (all metadata at the front), H is the end of the decoy's data region, D − 1 MiB.
* **Overlap check.** Before writing anything, hidden setup checks that no decoy LV other than the
  pool's data lies between the start of the pool's main data segment and the top of the hidden boot
  volume.
* **Both plain volumes are 255 MiB (522240 sectors)**, so they differ only in their start sector: 2048
  for the decoy, (H − 255 MiB)/512 for the hidden side. That is what makes the hidden boot a one-number
  `sed`.
* **The hidden data region never covers the decoy's plain volume or the header.** Even if the
  hidden side overflows, it can't break the decoy's ability to boot. A decoy that fails to boot would be
  suspicious in itself.
* **Memorable offset** (`memorable_offset`, on by default). The hidden plain volume's start is rounded
  down to a multiple of 10⁶ sectors (at most about 488 MiB of slack), so you remember "1945 million"
  instead of a random 10-digit number. This costs nothing in security: the offset is computable from
  the decoy's layout anyway, and what hides the hidden side is that it's indistinguishable from the
  random fill ([threat-model.md](threat-model.md#a2-coercer-in-scope-the-main-design-target)).

v1 differs: the hidden plain volume is at [D − 256 MiB, D − 1 MiB), and the hidden data region is
[0, D − 256 MiB), which covers the decoy's plain volume. The decoy pool's metadata sits at the front,
because v1 creates the pool with one data extent and then extends it.

---

## 4. Keys and encryption

| Key | Derivation | Opens |
|---|---|---|
| plain key | Argon2id(password, salt), 64 bytes (defaults: t=1, m=1 GiB, p=4, v1.3), or with FIDO2 a random key unwrapped with a security key (§4.1) | the side's plain volume |
| `data.key` | 64 random bytes generated at setup, stored in the plain volume | the side's data region |

* **Cipher.** Everything uses plain dm-crypt (aes-xts-plain64, 512-bit keys, 4096-byte sectors), so
  there's no header anywhere on disk to find.
* **Argon2 parameters: a short boot, a long password.** The defaults (one pass over 1 GiB, 4 lanes)
  take about 0.3 s on a current x86 machine, and about 1 s single-threaded through OpenSSL for stock boot
  ([stock-boot.md](stock-boot.md)). One pass with a lot of memory is the most memory-hard setting per
  second, which is what limits a GPU's parallel guessing. Making the KDF 10× slower buys about what one
  more random character buys at 60×, so pd keeps boots fast and relies on password length.
  `pd rekey --new-argon2 T,M_LOG2,P` changes them later (both sides; other parameters than the
  defaults go next to the boot image as `argon2.cred`, §7.1).
* **Salt.** The 32 bytes just below 1 MiB on the disk, as hex, shared by both sides (§4.2). The boot
  image carries none, so it's the same for every install.
* **Passwords must differ.** Setup enforces this, and checks that the two passwords don't derive the
  same key. (v1 instead appended a fixed suffix `a` to the salt for the hidden side. The
  suffix is public, so it added nothing.)
* **Changing a password** (`pd rekey`) re-creates that side's 255 MiB plain volume under the new key
  with the same contents (held in RAM, then compared after writing). `data.key` and the data region
  stay as they are. `pd header` gives the disk a new salt and re-keys both sides the same way (§4.2).
* **Why not LUKS.** v1 kept a detached LUKS2 header in the plain volume, using Argon2 output bytes
  64–511 as the keyslot passphrase. The motivation was reencryption and FIDO2, but neither was ever used.
  A coerced examiner walks the whole boot chain, so every layer has to be explainable, and a raw key file
  gives the same separation of password from data key. Dropping LUKS also removes the odd keyslot with a
  minimal PBKDF. FIDO2 (§4.1) works without it.
* **The hidden plain volume also stores the decoy's `data.key`,** so the hidden side can watch the
  decoy (§10.2).
* **Keys reach cryptsetup as files, never on stdin.** In plain mode, cryptsetup hashes a key read from
  stdin (`--key-file=-`) like a passphrase, but uses a key file's bytes directly. Setup passes keys as
  the read end of a pipe (`--key-file=/proc/self/fd/N`), so they never touch disk and match what the
  boot scripts read from tmpfs files.

### 4.1 FIDO2 security keys (optional)

A FIDO2 security key adds a second factor for a side's data. The boot volume stays keyed by
A = Argon2(password, salt), as without FIDO2; what the security key protects is the side's data-region
key, kept wrapped inside the boot volume. Nothing goes on the USB. It doesn't help against coercion
(A2): the security key is one more thing to hand over.

**What it protects, and what it doesn't.** Someone with a disk image can still check a password guess
offline by opening the boot volume, which shows that the side exists; the data stays out of reach
without the key. pd relies on long passwords to hide a side anyway (§4). Until D64, a second mode
protected the boot volume's key instead, so a guess needed the key too, but it needed a per-install
file on the USB, and such a file shows that an install exists, which a downloadable boot image
doesn't.

**Mechanism.** FIDO2's `hmac-secret` extension, as in `systemd-cryptenroll`, with its defaults:
non-resident credentials (nothing is stored on the security key), PIN and touch required, no user
verification. (U2F can't be used: it only makes randomized signatures, so it can't produce a stable
secret.)

| Value | Definition |
|---|---|
| A | Argon2id(password, salt), the boot volume's key |
| C_i | a credential created on security key i; its ID is an encrypted blob only that key can use |
| h_i | hmac-secret(C_i, SHA-256(A)): 32 bytes only security key i can compute, and only from A |
| SLOTn | secret_n XOR SHA-512(A ‖ h_i ‖ n), n the slot's number as one byte, so the two never share a pad |

The boot volume holds `fido2.txt` in place of `data.key` (and `decoy.key`): one line per credential,
`C_i SLOT0 SLOT1`. SLOT0 wraps the side's data key; SLOT1 the decoy's data key on the hidden side
(which the frontier monitor needs, §10.2), or random bytes on the decoy, so both sides' files look
alike. Several security keys mean several lines, each wrapping the same secrets. Losing every enrolled
security key loses the sides that use FIDO2; enroll at least two. Both sides can use the same keys.

**At boot,** `early.sh` opens the boot volume with A as without FIDO2. `late.sh` sees `fido2.txt`, asks
for the PIN, and runs `fido2-hmac unlock` (`tools/fido2_hmac.c`, static, in the boot volume), which
waits for a security key holding one of the credentials (checked silently, without a touch), gets
h_i (touch), and writes the two unwrapped keys to `/run/pd`. With a wrong key or none, the boot volume
opened, but the data region doesn't. Credential IDs are only readable behind the password, so whatever
structure they have can't confirm a guess that opening the boot volume doesn't already confirm.

**Commands** (`pd/rekey.py`, `pd/fido2.py`):
* `pd fido2 enroll [--offset N]` enrolls security keys for one side, one at a time: the plugged-in
  one, then each one the user swaps in when asked (one USB port is enough). A security key without a
  credential gets one. Once a side uses FIDO2, the first plugged-in key must be an enrolled one, to
  unwrap its keys. Enrolling rewrites the boot volume after refilling its whole area with random
  data, so the plaintext `data.key` doesn't survive in its free space (`test_fido2` checks).
* `pd rekey` re-wraps the slots under the new A for each security key in turn; keys that weren't
  plugged in stop working for that side. (The data key itself can't change without re-encrypting the
  data region; an old boot volume would have to come from a snapshot, which is out of scope.)
* `pd fido2 list` and `pd fido2 remove N`, per side (`--offset N`, and the side's password).
* Re-creating the decoy next to a hidden side re-wraps the hidden side's SLOT1 too (§9), so its
  security keys must be plugged in.

**Cost.** `fido2-hmac` (static libfido2, libcbor, OpenSSL; 5.7 MB) is in every boot volume; the USB
doesn't carry it. A PIN prompt and a touch on every boot of a FIDO2 side.

**Tests.** `tests/test_fido2.py` uses QEMU's emulated CanoKey, which implements `hmac-secret`. QEMU
emulates one CanoKey at a time, so the test swaps between two over QMP, like a user swapping keys. A
real security key can be passed into a VM with `-device usb-host`.

### 4.2 The salt and the disk's edges

The boot image is the same file for every install (§7.1), so the salt lives on the disk, next to the
data it protects (D55, `pd/header.py`).

* **The salt is the 32 bytes just below 1 MiB, `[1 MiB − 32, 1 MiB)`,** as 64 lowercase hex
  characters. Both sides use it. `pd randomize` fills it along with the rest of the disk, and setup
  reads it back: it's never chosen.
* **Why there.** Tools that write to the front of a disk leave it alone: an MBR (512 bytes), a primary
  GPT (17 KiB with 512-byte sectors, 24 KiB with 4 KiB sectors), Windows' "Initialize disk".
  Partitions conventionally start at 1 MiB, and partitioning tools don't write the gap below. A disk
  with no partition table shows as unallocated, and they write nothing until asked.
* **Why only 32 bytes, with no redundancy.** Hashing a larger range adds nothing (any changed byte
  changes the salt). Copies or error correction that anyone can decode would also let anyone check
  that they agree, which random data never does.
* **Typed in a live system:** `od -An -tx1 -v -j 1048544 -N 32 /dev/nvme0n1 | tr -d ' \n'`.
* **The edges look like nothing.** After the random fill, setup checks the first and last MiB with
  `pd/disk_class.sh` (the boot's own check, §7.2) and libblkid's full set of probes (`wipefs`), and
  refills an edge that matches anything. That happens about once in 65,536 disks, almost always for the
  MBR boot signature; nothing can observe a bias that small. (The setup kernel's partition parsers add
  nothing: nixpkgs enables only the MBR, GPT and LDM ones, all of which libblkid covers.)
* **Nothing is kept to prove the edges are filler** (D59): no seed, no fill key. The free space can never
  be proven unused either, so proving only the header would invite the question why, and the fill key
  would map every block the hidden side wrote (§2.4).
* **An explicit salt** in the config (`salt = …`) overrides the disk's, and goes next to the boot image
  as `salt.cred` (§7.1). Stock decoys need one (their first MiB is a partition table; [stock-boot.md](stock-boot.md)),
  and installs from before D55 have one until `pd header` moves it to the disk ([migration.md](migration.md)).
* **`pd header`** writes a new random header and re-keys both sides to it (it asks for both passwords).
  The old salt is saved next to the config first, so an interrupted run is finished by running it
  again. It leaves the trailer alone on layouts migrated from v1, whose decoy data region reaches the
  end of the disk, and refuses if a hidden side has data in the chunk holding the header (v1's hidden
  region starts at 0; it allocates that chunk last, after the decoy's whole region).

**Security.** Offline guessing no longer needs the USB: before D55, someone with only the disk couldn't
start guessing, because the salt was on the USB. The password carries the security either way
(whoever holds both could already guess), and FIDO2 adds a second factor (§4.1). In exchange the USB
proves less (a public file whose hash matches the release, not a record of an install), losing it
loses nothing, and the disk has no fixed pattern. Nothing changes for someone with the decoy password:
the decoy uses the salt itself, and it's the same for both sides (invariant 1).

---

## 5. The plain volume

The plain volume is a 255 MiB btrfs filesystem with `--data=dup` and blake2 checksums. That gives
integrity and self-healing for the most important 255 MiB on the disk without a dm-integrity layer. It
is mounted read-only during boot and never mounted by the running OSes.

```
late.sh                   stage-2 boot script
thin-ro.sh                read-only thin LV mapping (§7.3)
data.key                  this side's data-region key (with FIDO2: fido2.txt instead, §4.1)
table.txt                 dm table for this side's data region, with DISK standing for the disk
crypt.env                 cipher parameters handed to the target OS
bin/                      static lvm, dmsetup, pdata_tools (thin_check, thin_dump), kexec, fzf, jq, detect_shift,
                          tmeta_scan, fido2-hmac, busybox (the scripts' userland, so they also run from a
                          live system)
systems/<name>/env.sh     per-system settings: VG, root LV, kernel, initrd, cmdline
systems/<name>/extra.sh   per-system hook (e.g. the NixOS generation menu)
systems/default           symlink to the default system
hooks/frontier.sh         hidden side only: the frontier monitor (§10.2)
frontier.env              hidden side only: monitor threshold
decoy.key, decoy.table    hidden side only: used by the frontier monitor (with FIDO2, decoy.key is in fido2.txt)
```

`late.sh` sources every `hooks/*.sh` before handing over. The decoy has no `hooks/` directory; a test
checks that none of the hidden-only files exist in the decoy's boot volume.

Why split boot into early and late stages at all:
1. **The USB reveals as little as possible.** It holds only what's needed to find and open a plain
   volume: kernel, modules, busybox, argon2, cryptsetup. LVM, the thin tools, kexec, and every boot
   decision sit behind the password. This was the goal in 2024. It was given up when the static tools
   were dropped, and v2 restores it.
2. **The hidden side's boot logic has to live somewhere invisible.** The hidden plain volume is the only
   such place.
3. **Each side can carry its own tools and scripts** without any change to the USB.

---

## 6. The data region

Under the plain dm-crypt layer, each side has one LVM PV and one VG containing:
* a linear `swap` LV (hibernation isn't supported). It can't grow later, since the pool takes the rest of
  the VG, so by default it's the RAM size of the machine running setup, as an installer would choose
  (`swap_size`; `"0"` for none);
* a thin pool over the rest of the PV;
* thin LVs: one btrfs root per installed OS. The btrfs roots have the subvolumes `@`, `@boot`, `@home`,
  `@home_snapshots`, `@nix`, `@snapshots`, `@var_cache`, `@var_lib_docker`, `@var_log`, `@var_tmp`.
* optional extra thin LVs (`[[volumes]]` in the config, btrfs by default, or XFS), e.g. for bulk data like v1's
  ext4 `data` LV (§2.3 on choosing the filesystem). During setup they're mounted at
  `$PD_ROOT/volumes/<name>`.

The installed OSes' mount options are their own, except that every filesystem needs `nodiscard`
(§2.3); [install.md](install.md) has examples. During setup, pd mounts the roots with
`noatime,lazytime,nodiscard,compress=zstd:3`.

### 6.1 Decoy: stock LVM
The decoy is created only with stock, ordinary commands:
```
pvcreate
vgcreate
lvcreate -L … -n swap
lvcreate --type thin-pool -l 100%FREE --discards nopassdown -Zn -n thinpool
lvcreate -V … -T …/thinpool -n <root>
```
No explicit extent size, chunk size, or metadata size is given; LVM picks them for the disk size. Thin
LVs get the full pool size as their virtual size. Two options aren't defaults, both ordinary and
documented: `--discards nopassdown` (reclaim thin pool space without trimming the device) and `-Zn`
(don't zero new chunks, which LVM's own warning suggests for the large chunks it picks on big disks;
the filesystems never read blocks they haven't written).

**Why it matters: LVM metadata records its own history.** The metadata area is a ring buffer that keeps
many versions, and every version stores:
* the full command line that produced it (`description = "Write from lvcreate -L 64m -n swap vg1."`),
* `creation_host`, followed by a comment with the `uname` of the machine that ran the command,
* the PV's device path.

v1's decoy history shows things an ordinary install wouldn't: an explicit extent size, a one-extent pool
followed by `lvextend`, `pvmove`s, and device names like `/dev/mapper/luks_test_<uuid>`. v2's history
should look like an ordinary install. So setup also uses the device-mapper names the boot uses, and runs
under the decoy's hostname. Its own LVM settings (no backup or archive files, and the device filter
`pd move` needs) go into a private `lvm.conf` (`LVM_SYSTEM_DIR`), not onto the command line, which
therefore holds only the commands above with human sizes.

The `uname` comment does show the setup environment's kernel (a NixOS kernel of the version pd pins).
That isn't hidden: a pd decoy is recognizable as pd anyway (the pd USB, a disk without a partition
table), and the threat model only requires that nothing points to a *hidden* side. A stock decoy
([stock-boot.md](stock-boot.md)) gets no LVM metadata from pd at all.

### 6.2 Hidden: metadata at the front, capped thin LVs
Nobody sees the hidden VG, so its layout only has to be safe.
* **Metadata at the front.** The pool is created with one data extent and then extended
  (`lvcreate --type thin-pool -l 1 …`, then `lvextend -l +100%FREE`). This puts `tmeta` and `pmspare` at
  the start of the hidden PV: physically next to the hidden plain volume, as far as possible from where
  the two sides could meet. Stock placement would put the hidden pool's metadata exactly at the collision
  frontier.
* **Capped thin LVs.** Thin LVs are created with capped virtual sizes, e.g. 40% of the disk in total.
  A thin LV never maps more than its virtual size, so the hidden side can never occupy more than the sum
  of its virtual sizes plus swap and metadata, whatever its filesystems do. That is a hard guarantee
  that it never reaches the decoy (the cap covers extra volumes too, rounded down to whole extents).
  * Growing is an occasional, manual, online step (`lvextend` + `btrfs filesystem resize`), ideally
    after checking the frontier monitor. Nothing happens at boot.
  * The cap is on the thin LVs, not the pool, so a full hidden filesystem gets a clean ENOSPC. A full
    pool would instead make dm-thin queue or fail writes under a mounted filesystem.

---

## 7. Boot chain

```
firmware → UKI on the USB → /init → early.sh → (password, Argon2) → plain volume found
  → late.sh, from the plain volume → data region, LVM, @boot → kexec target kernel + initrd + key cpio
  → target initrd hook → data region → the distro mounts root as usual
```

### 7.1 UKI
A unified kernel image at `EFI/BOOT/BOOTX64.EFI` on the USB's ESP. This is the removable-media path, so
no NVRAM entry is needed. It's the same file for every install (D55, D58): Nix builds it (`bootUki`)
reproducibly, so a release can publish it with its hash, and a download can be checked against a build
(`nix build --rebuild`). It contains:
* **Kernel:** a nixpkgs kernel with the stock config, plus a broad set of storage, input, USB, and HID
  modules loaded through modalias coldplug. A missing keyboard driver means a machine that can't boot,
  so hardware coverage matters more than size.
* **initrd** (about 10 MB): static busybox, cryptsetup, argon2, and the kernel modules (stored
  uncompressed, because busybox `modprobe` can't reliably load compressed ones), plus `/init`,
  `early.sh`, `disk_class.sh` and `early_env.sh`. GPU drivers are left
  out (their firmware is over a gigabyte); the firmware framebuffer is enough for a text console.
  `/sbin` links to `/bin`, because the kernel runs `/sbin/modprobe` to load crypto modules on demand.
* **Command line:** includes `init_on_free=1`. Pages the boot environment frees (password buffers,
  Argon2 memory) are then zeroed before `kexec` hands RAM to the next kernel.

**Per-install files go next to it**, in `EFI/BOOT/BOOTX64.EFI.extra.d/`, so the image itself never
changes. systemd-stub packs the `*.cred` files it finds there into a cpio under `/.extra/credentials/`
in the initrd. `pd usb --config` writes them (`pd/usb.py`):

| File | Contents | Reveals |
|---|---|---|
| `keymap.cred` | the console keymap (busybox `loadkmap` format, built from kbd's keymaps) | the user's keyboard layout |
| `argon2.cred` | Argon2 parameters other than the defaults (the tests use this) | an install with custom parameters |
| `salt.cred` | a salt from the config (§4.2) | an install |

A USB without FIDO2, custom parameters or an explicit salt is the release file plus, at most, a keymap.
The Argon2 defaults are fixed per release: changing them would make installs re-key.

**Why a UKI booting full Linux rather than GRUB:**
* No dependence on what a bootloader supports (FIDO2, filesystems, LVM thin).
* The target kernel is loaded with `kexec`, so the USB doesn't need updating when the installed OSes
  update their kernels.
* The two sides can run different kernels.

v1 differs: the UKI is built by the host's dracut from the host's kernel, with udev, bash, and many
tools.

### 7.2 early.sh (on the USB)
`/init` mounts the pseudo-filesystems and starts busybox `mdev -d`, which loads the driver for every
device that appears (via its `MODALIAS` uevent), including keyboards that enumerate late behind hubs or
docks. It also makes a pass over the devices already present, repeated while new drivers keep exposing
new devices. It loads the configured console keymap (`keymap.cred`, with busybox `loadkmap`), then runs
`early.sh` and, if it returns, the recovery shell, in a single session (so no console output is lost
when a session leader exits).
1. Delete the `BootCurrent` NVRAM entry and remove it from `BootOrder`. This is done directly through
   efivarfs with busybox, since efibootmgr doesn't build statically; one tool fewer on the USB.
2. Prompt `Password:` with echo off (`stty -echo`). An empty password goes straight to the recovery
   shell without running Argon2: that's the start of the hidden boot.
3. Sort the whole disks in `/sys/block` (there's no udev) with `disk_class.sh`, the check setup also
   runs on the disk's edges (§4.2, D57):

   | Disk | Treatment |
   |---|---|
   | loop, ram, zram, dm, sr; smaller than 2 GiB (setup's minimum) | never tried |
   | salt bytes all the same value (zeros on a new or trimmed SSD, `0xff` on erased flash) | never tried: 32 random bytes never are, and a disk whose salt was overwritten can't boot anyway |
   | looks partitioned or formatted | tried last |
   | everything else | tried first |

   Largest first within each group. A disk looks partitioned or formatted if the kernel created
   partitions for it, bytes 510–511 are `55 AA`, `EFI PART` sits at byte 512 or 4096, or `blkid` names
   a type. The boot USB itself is never tried (the bytes just below its ESP at 1 MiB are zeros) or
   tried last. Partitioned-looking disks are tried last rather than skipped, so a pd disk whose front
   sectors something overwrote still boots (its salt is at the end of the first MiB); only wrong
   passwords pay for them. `PD_DISK=/dev/nvme0n1 /early.sh` in the recovery shell tries just that disk.
4. For each candidate: read its salt (or `salt.cred`'s), run `argon2` (reusing the key if the previous
   disk had the same salt), and with it:
   * `cryptsetup open --type plain --readonly --offset $PLAIN_OFFSET --size $PLAIN_SIZE …`;
   * mount btrfs with `ro,rescue=nologreplay`;
   * `exec` `late.sh` from it. Using `exec` frees early.sh's memory, password included, which
     `init_on_free=1` then zeroes.
5. If no disk works, shred the keys and drop to a shell: the "recovery shell".

Each disk tried costs one Argon2 run (about 0.3 s): with the right password on a machine with one pd
disk, that's one, as before D55. The password stays in the shell's memory
until a disk opens or all have failed. After `kexec` nothing changes: the target's initrd hook already
finds the disk by trying the data key on every disk (§7.4), without udev or Argon2.

`early_env.sh` holds `PLAIN_OFFSET=2048`, `PLAIN_SIZE=522240`, and the Argon2 and cipher parameters. The
hidden boot edits `PLAIN_OFFSET` ([booting.md](booting.md)). (v1 instead used a dm table, `table.txt`,
for the plain volume.)

### 7.3 late.sh (in the plain volume)
1. **Pick the system:** `$LATE` if set (used by tests); otherwise, if Shift is held, show a menu
   (`detect_shift` + `fzf`); otherwise `systems/default`.
2. **Open the data region:** `dmsetup create --readonly` from `table.txt`, then
   `cryptsetup open --type plain --readonly` with `data.key`.
3. **Map the root LV read-only, without dm-thin.** The kernel refuses dm-thin in a read-only table
   ("thin-pool: target type may not be included in a read-only table"), and activating the pool
   read-write could write pool metadata. So `thin-ro.sh` reads the LVM layout with `lvm --readonly`
   (no activation), maps the pool's `tmeta` and `tdata` as read-only linear devices, dumps the root LV's
   block mappings with `thin_dump`, and builds an equivalent read-only table: `linear` onto `tdata`
   for mapped blocks, `zero` for the rest. Every LVM call passes `--devices`, because on a hidden boot
   with the frontier monitor both VGs share a name.
4. **Mount the boot files:** the root's `@boot` (and `@nix` for NixOS) from that mapping, read-only
   with `rescue=nologreplay`. Then run `systems/<name>/extra.sh` and any `hooks/*.sh`.
5. **Build the initrd:** copy the target initrd, pad it to a multiple of 4 bytes, and append a cpio
   containing `/boot_tmp/{data.key, table.txt, crypt.env}`.
6. **Hand over:** `kexec -l` the target kernel with that initrd and the system's command line, shred the
   temporary files, then `kexec -e`.

**The boot environment never writes to the internal disk.** Every mapping it creates is read-only, and
a test boots it with the disk attached read-only all the way to `kexec`. This has a consequence: btrfs
refuses to replay a dirty log tree on read-only media (`btrfs_replay_log()`: "log replay required on RO
media"), so after an unclean shutdown a plain `ro` mount would fail. With `rescue=nologreplay`, the mount
succeeds, and the target OS replays the log when it mounts read-write. A test leaves a dirty log tree in
`@boot` (confirmed via `log_root` in the superblock) and boots.

A dirty log tree is common: any `fsync` anywhere in the filesystem (not just in `@boot`) followed by a
crash or power loss leaves one, and mounting any subvolume needs it replayed. Measured on a crashed
test disk:

| Device | Mount options | Result |
|---|---|---|
| read-only | `ro` | fails ("can't read superblock"); the boot would stop |
| read-only | `ro,rescue=nologreplay` | mounts; nothing written |
| writable | `ro` | mounts, **but replays the log and writes to the disk** (`log_root` becomes 0) |

The last row is v1's situation (LVs activated read-write): after a crash, v1's boot environment
writes to the internal disk despite mounting `ro`. Any `rescue=` option also makes btrfs
refuse transactions, which the kernel log shows as `state E`; that is expected.

v1 differs: it has no `nologreplay`, and it activates LVs read-write on top of a read-only crypt device.

### 7.4 Target initrd hook
One script, `target/hook.sh`, is shared by every target OS. NixOS runs it as a systemd initrd service
(`target/nixos/pd.nix`, tested with a real NixOS initrd); Arch as a mkinitcpio hook for the busybox
initramfs (`target/arch/`, tested with a real Arch install). Both integrations also install the
shutdown trim (§2.3); the NixOS module has `pd.v1Compat` for migrating v1 installs.
1. Wait for devices to settle.
2. **Find the disk by trial.** For each disk, create the `table.txt` mapping on it and open plain
   dm-crypt with `data.key`. Keep the disk whose decrypted data region starts with an LVM label
   (`LABELONE`). The boot USB has no udev, so it can't pass on a `/dev/disk/by-id` name the way v1 did.
3. Shred `/boot_tmp`.

The distro's own initrd then activates LVM and mounts root as usual.

---

## 8. Multiple systems per side

Each side can hold several OSes in the same VG (e.g. Arch and NixOS).
* `systems/<name>/env.sh` sets `VG`, `LV_ROOT`, `KERNEL`, `INITRD`, and `CMDLINE`.
* `systems/default` points at the default system.
* Holding Shift while the plain volume opens shows a system menu.
* For NixOS, `extra.sh` mounts the `@nix` subvolume at `/nix` inside the boot environment, so the
  generation symlinks into `/nix/store` resolve. Holding Shift again shows a generation menu with a
  `boot.json` preview. The chosen generation supplies the kernel, initrd, `init=`, and kernel
  parameters.

---

## 9. Setup

* **Throwaway environments only:** the tool VM for tests, and for real installs the RAM-only setup
  UKI (`setupUki`: pd, LVM, cryptsetup, btrfs and XFS tools, plus `pacstrap`, `nixos-install` and
  `pd-net` for installing the OSes). Running setup on a daily-driver machine leaves traces of **both**
  sides:
  * LVM writes copies of every VG it touches to `/etc/lvm/archive/` and `/etc/lvm/backup/`;
  * the journal records every filesystem UUID that gets mounted.

  Setup also disables LVM archive and backup explicitly.
* **Keys only travel through pipes or memory**, never temporary files.
* **Order:** randomize the whole disk (edges checked and refilled if they look like anything, §4.2) →
  create the decoy, keyed under the salt read back from the first MiB → read back the decoy's layout to
  find H → create the hidden side. `init-decoy` gives an unrandomized disk random edges itself.
* **pd's core stops at "this side's roots are mounted".** `pd init-decoy/init-hidden --run` opens a
  shell with each system's root at `$PD_ROOT/<system>`; the OS is installed with the distro's own tools
  ([install.md](install.md)).
* **Stock decoy:** `pd init-hidden --stock-decoy DEVICE` puts the hidden side into a stock install's
  LUKS + LVM thin layout instead of a pd decoy, opening the decoy only read-only; the hidden side is then
  booted from a live system ([stock-boot.md](stock-boot.md)).
* **Commands:** `pd randomize`, `pd init-decoy`, `pd init-hidden`, `pd compact`, `pd move`, `pd open`,
  `pd close`, `pd rekey`, `pd header`, `pd usb`, `pd install-cpio`, `pd migrate-v1`, `pd fido2`, `pd salt`
  (for a config's explicit salt) (`python3 -m pd --help`).
* **`pd open` / `pd close`** (`pd/opener.py`) work from the setup UKI or a live ISO (`pd.pyz`, a zipapp,
  with distribution tools). Read-only by default, using the boot environment's approach (read-only
  mappings, thin LVs mapped from `thin_dump`, `rescue=nologreplay` for btrfs, `norecovery` for XFS);
  `--rw` activates the VG. Undo steps are recorded in `/run/pd/open.json` (no keys) for `pd close`.
* **Re-initializing a decoy** is refused if the decoy password already opens a boot volume. With
  `--force`, `init-decoy` needs `--no-hidden` or `--hidden-offset` (and the hidden password); with a
  hidden side, the new decoy pool's metadata goes at the front instead of stock placement (which would
  put `tmeta` at the end, possibly on a v1 hidden boot volume), and the layout is checked against the
  hidden side before the data region is written. The disk is never re-randomized. The hidden side's
  copies of the decoy's data key and table (the frontier monitor's) are then replaced with the new
  ones; with FIDO2 that needs the hidden side's security keys.
* **The UKI records nothing about the build host:** `pd usb` passes a neutral `--os-release`
  (ukify otherwise embeds the host's `/usr/lib/os-release`).
* **Writing the USB.** `pd usb --output ESP [--config pd.toml]` copies the release boot image to the
  stick's ESP and writes the per-install files next to it (§7.1). The setup UKI carries the boot image
  and the keymaps (`$PD_BOOT_ENV`), so it needs nothing else. Updating the boot environment is replacing
  one file. Per-install values never live in the repo or in `/nix/store`.
* **Per-install configuration** (disk, VG names, sizes, Argon2 parameters) is a TOML file kept outside
  the repo.
* **The hidden offset is printed once.** It must not be written on the USB or anywhere in the decoy.

---

## 10. Space management and safety

### 10.1 The user's responsibility
Nothing stops one side from overwriting the other if the two footprints together exceed the disk.
* **The decoy can't be capped.** It has to look like a whole-disk volume, and an ordinary setup wouldn't
  stop at an arbitrary point.
* **The hidden side is capped** (§6.2).
* **Footprints are high-water marks** (§2.3): with the shutdown trim they stay near live data plus
  one session's writes; without it they only grow.
* **An overwrite is visible.** If the hidden side ever overwrote decoy data, the decoy would show btrfs
  checksum errors, which is both damaging and suspicious.

### 10.2 Frontier monitor
`hooks/frontier.sh` exists only in the hidden boot volume and runs on every hidden boot. It opens the
decoy's data region read-only with the stored decoy `data.key`, finds the highest allocated data block of
each pool with `thin_dump`, maps both through the LVM segments and the region tables to physical
offsets, and prints `Free space between the two sides: N MiB.` Below `MIN_GAP_PERCENT` (default 10% of
the disk, in `frontier.env`) it prints a warning and waits for Enter. It never writes to either side.
Tested: 7379 MiB free on a fresh 8 GiB test disk; 543 MiB and a warning after writing 6.5 GiB to the
decoy.

It also warns (and waits) if any decoy LV other than the pool's data lies inside the hidden region, e.g.
after someone changed the decoy's LVM layout; if the decoy doesn't open with the key the hidden side
keeps for it (re-created without `--hidden-offset`); if the decoy's data is above the hidden side's
floor; and
if the decoy's pool metadata, stale nodes included, points above the floor or into the hidden side's
data (§10.5).

### 10.3 Never clone the disk
A snapshot adversary who compares two images can see "unused" space change. Backups of hidden data must
go to another pd setup; never `dd` the disk. To move to another disk, use `pd move` (§10.4).

### 10.4 Compacting and moving
**Compacting a pool in place** (`pd compact`, `pd/compact.py`). A pool that was once nearly full keeps
allocations spread over its whole data area, even after the data is deleted and trimmed: dm-thin never
moves data. A hidden side needs the top of the decoy's pool data free, so `pd init-hidden` checks the
decoy's highest allocated block and refuses if it's above the **floor**: the lowest point a hidden
side at its cap can reach (hidden boot volume − cap × disk − swap − 1 GiB for the hidden PV's
metadata). `pd compact` then moves the decoy's data below the floor:
1. It fstrims every thin LV's filesystem, so the pool unmaps what they've freed (`nopassdown`: nothing
   reaches the disk).
2. With the pool inactive, it maps the pool's metadata and data directly (dm-linear over the LVM
   segments) and runs `thin_shrink` (thin-provisioning-tools): every mapped block above the boundary is
   copied into a free block below it, and the metadata is rewritten to match. The pool keeps its
   size, and LVM's metadata doesn't change.
3. The new metadata goes to LVM's spare metadata LV (`pmspare`) first and is checked. Then the pool's
   metadata LV is filled with random data and the new metadata written over it (§10.5). Until then
   the pool is unchanged, since data was only copied into unused blocks. If the final write is
   interrupted, `pd compact --recover` restores the metadata from the spare. Afterwards the spare is
   filled with random data, so a stale copy is never mistaken for a good one.
4. For the decoy, every free pool block above the boundary is refilled with random data (§10.5).

Destinations are always below the side's own highest block, and without a collision the other side's
data lies entirely beyond it. The refill, though, covers free space up to the hidden side, so
compacting the decoy needs `--no-hidden` (no hidden side exists yet) or `--hidden-offset N` (asks for
the hidden password too, and stops the refill one 256 MiB chunk below the hidden side's lowest
block). The flag is required rather than defaulted, so forgetting a hidden side can't destroy it.
`--below SIZE` sets the boundary explicitly; the default for the hidden side is as tight as possible.
`pd compact --stock-decoy` does the same for a stock decoy ([stock-boot.md](stock-boot.md)), whose
`init-hidden` checks the floor too. *(tested: `test_compact`, for a pd decoy, including `--recover`
after a damaged metadata write and a scan for the fill's marked blocks above the boundary; for a
migrated v1 decoy; and for a stock LUKS decoy.)*

**Moving to another disk** (`pd move`, `pd/move.py`), bigger or smaller. The old disk is only read.
The new disk gets fresh layouts built like init-decoy and init-hidden (new data keys, the hidden side at
a new memorable offset), and each thin LV's mapped blocks are copied at the same offsets, read through
the old pools' metadata without activating them. Filesystems and their UUIDs come over unchanged, the
new pools fill from the front, swap is recreated with its UUID, and the thin LVs get the usual sizes
for the new disk: on a bigger one, btrfs and XFS filesystems grow; on a smaller one, btrfs filesystems
shrink (XFS can't, so its LV keeps its virtual size). The
salt and Argon2 parameters stay, so the boot USB still works; FIDO2 has to be enrolled again. Then
wipe the old disk. *(tested: `test_move`, 8 → 12 → 6 GiB, both sides booted after each move.)*

### 10.5 Traces of the decoy's old data
The floor check (§10.4) and the frontier monitor look at where the decoy's data *is*. An examiner with
the decoy password can also see where it *was*:
* **Freed data blocks keep their contents.** No discard reaches the disk (§2.3), so a block the decoy
  trimmed still holds its old ciphertext, which decrypts to recognizable data (btrfs metadata blocks,
  for one, carry the filesystem's UUID, their own address and a checksum).
* **The pool's metadata keeps old btree nodes.** dm-thin's metadata is copy-on-write; superseded nodes
  stay in its free blocks until reused, and `thin_scan` or `thin_dump --repair` finds them. They say
  where the decoy's blocks used to be.

If the hidden side later overwrote such a block, the decoy's old data there would have turned into
noise, which nothing on the decoy side explains. The invariant that prevents it: **the decoy's data
never rises above the floor** of the hidden side's region. Then all its traces point into space the
hidden side never takes. Two things break the invariant, and pd clears the traces in both:
* **`pd compact` on the decoy** moves data out of the space above the boundary on purpose. It rewrites
  the pool's metadata over random data (old nodes gone; the spare, which held a copy, is filled with
  random data afterwards) and refills every free pool block above the boundary, up to the hidden side
  if there is one, with random data. The refill is written to the raw disk through a dm-crypt mapping
  with a throwaway key (`pd-wipe`), so it looks like the random fill of a never-used disk (§2.4):
  random data written through the decoy's own key would also do, but zeros through it would decrypt
  to zeros.
* **`pd init-hidden` on a decoy that has been used.** A decoy installed in the same setup session
  (its pool was created since the machine booted, from LVM's `lv_time` and `/proc/stat`'s `btime`)
  has never been trimmed, so everything it ever wrote is still mapped and the floor check covers it.
  Otherwise, and always for a stock decoy, `init-hidden` does the same rewrite and refill above the
  floor first. On a large disk that writes cap × disk and takes a while.

**Between hidden boots**, the decoy can grow above the floor and shrink again (the shutdown trim),
leaving traces in the hidden side's future space. The frontier monitor catches both on the next hidden
boot (D61):
* the decoy's data is above the floor now (`FLOOR_SECTOR` in `frontier.env`, from `thin_dump`);
* its pool metadata points above the floor: `tmeta_scan` (`tools/tmeta_scan.c`, static, in the hidden
  boot volume) reads the decoy's whole metadata device and takes the highest data block that any valid
  btree leaf maps, stale copies included. A leaf is valid if its checksum (crc32c, as
  dm-persistent-data computes it) and its own block number match; bottom-level mapping values are
  `data block << 24 | time`, and the top-level tree's values (metadata block numbers) read as block 0.
  If those pointers reach the hidden side's lowest block, the evidence exists already, and the warning
  says so.

Either way, `pd compact --hidden-offset N` on the decoy clears the traces. What `tmeta_scan` can't see
is a stale node that has since been reused: the decoy's old data in freed blocks then stays, with
nothing left pointing at it. An examiner could still find recognizable old decoy blocks there, but
couldn't show that any are missing. Scanning the metadata takes about a second per GiB of `tmeta`.

---

## 11. Build and test

* **Build.** A Nix flake pins nixpkgs and builds:
  * static busybox, cryptsetup, argon2 (boot USB); static lvm2, thin-provisioning-tools, kexec-tools,
    fzf, jq, and detect_shift (boot volume);
  * the kernel and its module closure, the boot initrd, and the release boot image (`bootUki`,
    reproducible: `nix build --rebuild` checks it, and `test_uefi` rebuilds it with ukify and compares);
  * the tool VM (kernel + tiny initrd that mounts the host's `/nix/store` read-only over 9p, so tools
    aren't copied) and a fake target OS;
  * a minimal NixOS system using `target/nixos/pd.nix`, for integration tests;
  * the RAM-only setup UKI (`setupUki`), which carries the boot image and keymaps for `pd usb`.

  Build with `nix build "path:$PWD#testenv"` (and `#setupUki`, `#bootUki`; `#release` is everything
  a release publishes, [release.md](release.md)). The `path:` form is needed because Nix's libgit2 can't
  read repositories using the reftable ref format.
* **Test.** Tests run QEMU (KVM, OVMF) as an ordinary user against sparse image files. Setup runs as
  root inside the tool VM; nothing needs root on the host, and no device-mapper state is created there.
  Run `python3 -m unittest discover -s tests -t .`.
* **Suite** (`tests/`):

  | Test | Covers |
  |---|---|
  | `test_layout`, `test_placement`, `test_usb`, `test_monitor_awk` | unit tests: layout arithmetic, reversed tables, hidden placement and overlap checks, the per-install files next to the boot image, `disk_class.sh` on image files (blank, small, MBR signature, GPT header, a filesystem), salt read-back from old boot images, the monitor's overlap detection |
  | `test_smoke` | the tool VM |
  | `test_e2e` | decoy boot, hidden boot via the recovery-shell `sed`, wrong password, password not echoed, read-only disk, keyboard plugged in after boot, keymap; the release's `pd-live` bundle boots both sides from the tool VM; with an empty and a small extra disk and the pd disk's copy carrying an MBR signature and a GPT header, the classes are blank/small/used and the decoy still boots |
  | `test_safety` | boot after a crash with a dirty btrfs log; hidden cap over roots and volumes; fstrim shrinks the pool but never reaches the disk; random edges that nothing recognizes |
  | `test_growth` | on an unrandomized disk, decoy writes at the front, hidden at the back, untouched gap between (and the random edges `init-decoy` gave it) |
  | `test_uefi` | the release boot image is reproducible (rebuilt bit for bit) and has no build-host os-release; the same image with a serial console boots from a USB stick under OVMF with `argon2.cred` in `BOOTX64.EFI.extra.d/` (the decoy only opens with it); NVRAM entry and `BootOrder` cleanup |
  | `test_setup_env` | setup run from the RAM-only setup environment, `pd usb` from its own boot image, then a boot; with `PD_TEST_NET=1`, DHCP through an emulated Intel NIC and a signature-checked `pacstrap` |
  | `test_nixos` | a real NixOS initrd with `pd.nix` unlocks the disk and mounts `/sysroot` |
  | `test_arch` | a real Arch install (pacstrap, `pd` mkinitcpio hook) boots through pd (`PD_TEST_ARCH=1`, needs network) |
  | `test_monitor` | frontier monitor report and warnings (decoy above the floor, small gap; after the decoy deletes and trims, its stale metadata above the floor; none after `pd compact`); no hidden-only files in the decoy |
  | `test_stock` | stock boot: a stock LUKS + LVM thin decoy's header, LVM metadata and files are left unchanged by `init-hidden --stock-decoy`, and the printed commands boot the hidden side from another VM |
  | `test_live` (opt-in: `PD_TEST_LIVE=1`) | stock boot from the real Debian Live 13 Xfce image: log in on its serial console, type the printed commands, boot the hidden side |
  | `test_compact` | a pd decoy, a migrated v1 decoy and a stock decoy, filled and mostly emptied: init-hidden refuses, `pd compact` moves the data down (files intact; `--recover` after a damaged metadata write; none of the fill's marked blocks left above the boundary), then both sides boot; `init-hidden` on a decoy that used and trimmed the hidden side's space clears it (§10.5) |
  | `test_move` | `pd move` 8 → 12 → 6 GiB: files intact, btrfs roots grown and shrunk, both sides boot after each move |
  | `test_fido2` | enroll two emulated security keys for both sides, boot with either, wrong PIN, unknown key, rekey, remove; nothing for the USB, and the plaintext data key is gone from the boot volume |
  | `test_churn` | btrfs, XFS and (unsupported) ext4 pool footprint under churn, with and without a shutdown trim |
  | `fs_survey.py` (not in the suite; `python3 -m tests.fs_survey`) | the §2.3 table: filesystems and btrfs options × workloads × trim |
  | `test_luks_equiv` | a v1 LUKS2 data region reads identically through plain dm-crypt with its volume key |
  | `test_migrate` | a v1-format disk migrated with `pd migrate-v1` keeping an explicit salt, then moved to the disk salt with `pd header`, boots both sides with v2 (`test_compact` migrates straight to the disk salt) |
  | `test_rekey` | new passwords and Argon2 parameters on both sides, then a new header (`pd header`); `--new-salt` is refused without an explicit salt; both boot with a new USB, the old password fails |
  | `test_reinit` | re-initializing the decoy is refused without `--force`; with the hidden offset, both sides still boot, and the hidden side's frontier monitor reads the new decoy |
  | `test_open` | `pd.pyz open`/`close`: read-only with the disk attached read-only, hidden side, read-write, wrong password |

---

## 12. Alternatives considered

| Alternative | Verdict |
|---|---|
| VeraCrypt hidden volumes | The feature itself raises the question pd tries to avoid. |
| Discards on, with the pool in `nopassdown` mode | Rejected: churn walks the next-fit cursor into the other side (§2.3). |
| Reversal inside LVM (descending PE ranges) | Rejected: the hidden PV label and metadata would sit at the collision frontier. |
| LUKS2 with a segment offset for the decoy (stock crypttab) | Rejected: LUKS is dropped, and decoy and hidden would boot differently. |
| LUKS with a detached header in the plain volume | v1. Dropped: its features went unused, and it's one more layer to explain. |
| GRUB | Rejected: a full Linux boot environment is more flexible and decouples the USB from installed kernels. |
| dracut-built boot environment (host kernel + host tools) | v1. Replaced by a pinned minimal build, so the USB reveals less. |
| mkinitcpio → dracut for the Arch target | Tried and reverted: it added nothing, and mkinitcpio is simpler and the Arch default. |
| dm-integrity under the plain volume | Replaced in 2024 by btrfs dup + checksums. |
| Salt on the USB | v2 until D55: one key derivation tried every disk, but the USB belonged to one install and held the salt's only copy besides the config. |
| Salt = hash of the whole first MiB | Any changed byte destroys it; nothing gained over 32 bytes. |
| Redundant, recoverable salt | Redundancy that anyone can decode, anyone can check: the first MiB would no longer look random. |
| First MiB generated from a stored seed, to prove it hides nothing | The free space can't be proven unused anyway (D59). |
| Skip empty disks by a zero first sector | Weaker than checking the salt bytes: misses disks that are zero only at the salt, and skips a pd disk whose first sector was overwritten. |
| Skip partitioned-looking disks at boot | A pd disk whose front sectors were overwritten would never boot. Tried last, they cost only wrong passwords. |
| Ask for the disk at boot | Not needed: the order finds it, and after kexec the hook scans with the data key anyway. The recovery shell can still name one (`PD_DISK`). |
| One FIDO2 touch for all disks (the security key's input derived from the password alone) | Changes the FIDO2 format and adds an Argon2 run, while one touch per candidate rarely means more than one. |
| Hidden salt suffix | Removed: it's public, so it adds nothing. Setup enforces different passwords instead. |
| `pvmove` to tidy the pool layout (`MOVE_PES`) | Removed: cosmetic, and visible in LVM history. The decoy layout is now stock. |
| rdseed as an extra entropy source | Removed: the kernel CSPRNG is enough. |
| Secure Boot | Not planned: it ties the disk to one machine. |
| Stock decoy boot + hidden boot typed from memory in a live ISO | Documented as [stock boot](stock-boot.md); pd only sets up the hidden side (D39). |

---

## 13. Decision log

These decisions come from the design review held when v2 started. "Agreed" means the author confirmed
it.

| # | Decision | Why | Status |
|---|---|---|---|
| D1 | No discard reaches the disk (dm-crypt without `allow_discards`); filesystems `nodiscard`; pools `--discards=nopassdown`; one `fstrim` at shutdown | Deniability, and bounded growth (§2.3) | agreed (revised: originally `--discards=ignore`, which made a shutdown trim impossible; measurements show the shutdown trim keeps footprints near live data) |
| D2 | Hidden data reversed in 256 MiB chunks, from 256 MiB up to the hidden plain volume; fix `reversed_table`'s offset bug (it ignores `start` when `start != 0`) | The hidden side grows from the back and can't destroy the decoy's boot volume | agreed |
| D3 | Decoy data region is [256 MiB, D) | An ordinary install uses the whole disk | agreed |
| D4 | First MiB zeroed | No accidental signatures; plain volume starts at sector 2048 | superseded by D55, D56 (random, checked for signatures) |
| D5 | Shred key never printed; read from `/dev/urandom` by cryptsetup | The shred key would reveal every write | agreed |
| D6 | No key material in temporary files | Temp directories may be disk-backed | agreed |
| D7 | Random salt by default | A static default would allow precomputation across users | agreed |
| D8 | Drop the hidden salt suffix; enforce decoy ≠ hidden password | The suffix adds nothing | agreed |
| D9 | `init_on_free=1` on the boot command line | Reduces key residue across kexec | agreed |
| D10 | Plain dm-crypt + `data.key` instead of LUKS | Fewer layers to explain, same decoupling of password and data key | agreed |
| D11 | One shared target hook for all OSes | Uniform boot | agreed |
| D12 | early.sh uses `cryptsetup --offset/--size`; the hidden `sed` edits `PLAIN_OFFSET` | No dmsetup on the USB; shorter manual path | agreed |
| D13 | Minimal USB: kernel, modules, busybox, argon2, cryptsetup | Reveals little without a password | agreed |
| D14 | Delete `MOVE_PES` | Cosmetic; superseded by D19 | agreed |
| D15 | Hidden-side frontier monitor | Prevents overwrites | agreed |
| D16 | Hidden thin LVs capped by virtual size | Hard bound on the hidden footprint | agreed |
| D17 | Nix-pinned build; rootless QEMU tests | Reproducible; no host root or host dm state | agreed |
| D18 | A churn test decides the `data` LV's filesystem | ext4 may not reuse addresses | agreed; result in §2.3, D30 and D37 |
| D19 | Decoy LVM is stock; the hidden side sits below the decoy's end-of-disk metadata, read back rather than assumed | Stock layout and stock history | agreed |
| D20 | Decoy LVM history looks ordinary (stock commands, boot-time names, decoy hostname); no LVM archive or backup during setup | LVM metadata records commands, host, and device names | agreed |
| D21 | Boot-environment btrfs mounts use `rescue=nologreplay` | Booting after an unclean shutdown | agreed |
| D22 | Per-install config outside the repo; salt read back from the USB (`pd usb --salt-from`) | Keeps install values out of git and `/nix/store` | superseded by D55 |
| D23 | Setup only in throwaway environments | The setup host would otherwise keep traces of both sides | agreed |
| D24 | OS installation through optional recipes | Separation of concerns | agreed |
| D25 | The target hook finds the disk by trial | There's no udev on the USB | agreed |

Decisions made while implementing v2:

| # | Decision | Why | Status |
|---|---|---|---|
| D26 | The boot environment maps the root thin LV read-only from `thin_dump` output instead of activating the pool | dm-thin can't run in a read-only table; activating it read-write could write metadata | agreed |
| D27 | NVRAM cleanup writes efivarfs directly from busybox | static efibootmgr doesn't build; one tool fewer on the USB | agreed |
| D28 | An empty password goes straight to the recovery shell, skipping Argon2 | Faster hidden boot; a mistyped real password still takes the normal path | agreed |
| D29 | Keys are passed to cryptsetup as files (pipe fds), never on stdin | Plain mode hashes stdin keys; the boot scripts read raw key files | agreed |
| D30 | Root LVs are btrfs; extra data volumes are btrfs by default and may be ext4 | With the shutdown trim both stay near live data; ext4 avoids copy-on-write reallocation for files rewritten in place, but depends on the trim more (§2.3) | superseded by D37 |
| D31 | Hidden-only features use a generic `hooks/` directory in the boot volume | Keeps hidden-specific code out of `late.sh`, which is identical on both sides | agreed |
| D32 | Kernel modules in the boot initrd are uncompressed; GPU drivers omitted | busybox modprobe can't reliably load compressed modules; GPU firmware is over 1 GB | agreed |
| D33 | Builds use `path:` flake references | Nix's libgit2 can't read reftable repositories | agreed |
| D34 | Target OSes trim once at shutdown (NixOS `pd.trimOnShutdown`, default on; Arch unit in `target/arch/`); periodic fstrim disabled | Keeps footprints near live data (§2.3) | agreed |
| D35 | A RAM-only setup UKI (`setupUki`) for running setup on real hardware | A throwaway environment with nothing to install | agreed |
| D36 | Keep btrfs's default mount options; recommend `chattr +C` (or an XFS volume) for files rewritten in place | The filesystem survey (§2.3): `ssd`/`nossd`/`autodefrag` make no difference, `ssd_spread` is worse, and only rewrites of large files are a problem for btrfs | proposed |
| D37 | Extra volumes are XFS by default and may be btrfs; ext4 is dropped | The filesystem survey (§2.3): XFS matches ext4 on in-place rewrites and is much better under churn, with or without the trim | agreed; default superseded by D49 (ext4 stays dropped) |
| D38 | Optional FIDO2: hmac-secret unwraps a random plain key from per-key slots on the USB (two per credential, random when unused); systemd-cryptenroll's defaults (PIN and touch, no user verification) | Stops offline password guessing; nothing hidden-specific on the USB; any of several keys works; a side without FIDO2 still boots (§4.1) | agreed |
| D39 | Stock boot: `pd init-hidden --stock-decoy` adds a hidden side to a stock LUKS + LVM thin decoy; the hidden boot is typed in a live system (OpenSSL's Argon2id derives pd's key; `late.sh DISK` runs as on pd's USB); the salt may be the decoy's LUKS UUID | No pd USB to recognize; the hidden side and its boot logic are unchanged; nothing to bring but memory (stock-boot.md) | agreed |
| D40 | The project is called Plain Disguise; the tools stay `pd`. The name appears only in the docs, the flake and the CLI help, never on the USB or in the decoy | It hides in plain sight, in plain dm-crypt volumes; files on the USB and in the decoy already say only "pd" | agreed |
| D41 | Swap defaults to the RAM size of the machine running setup (`swap_size`, `"0"` for none) | The swap LV can't grow after setup, and an installer would size it that way; the old 64 MiB default looked odd | agreed |
| D42 | The last MiB of the disk is unused (zeroed until D56); the decoy data region is [256 MiB, D − 1 MiB) | A backup GPT written by a tool that initializes the disk can't hit the decoy's pool metadata (§3) | agreed |
| D43 | Argon2 defaults t=1, m=1 GiB, p=4 (about 0.3 s); `pd rekey --new-argon2` | Short boots, strong passwords: an extra random character is worth far more than a slower KDF; one pass over much memory is the most memory-hard per second (§4) | agreed |
| D44 | Thin pools are created with `-Zn` | Zeroing new chunks only slows first writes (LVM warns about it for large chunks); chunk size measured not to affect footprints (§2.3) | agreed |
| D45 | The setup UKI has drivers for common wired NICs and USB Ethernet adapters (the boot USB has none) | `pd-net` needs them on real machines; the boot USB never networks | agreed |
| D46 | The setup UKI ships Arch's keyring (pinned by hash) and a `pacman.conf` that requires signatures, with Arch's own mirror | `pacstrap` then verifies everything it installs; nixpkgs has no Arch keyring | agreed |
| D47 | `pd compact`: `thin_shrink` on the inactive pool, the new metadata staged in `pmspare`; `init-hidden` refuses a decoy with data above the hidden side's floor | A pool that was once full has data everywhere; dm-thin never moves it. Interruptions leave the pool intact or recoverable (§10.4) | agreed |
| D48 | `pd move` copies each thin LV's mapped blocks to fresh layouts on another disk, instead of growing or shrinking in place | Works for bigger and smaller disks, needs no change to the old disk, and compacts as a side effect; in place, the decoy's end-of-disk metadata and the hidden side would have to move (§10.4) | agreed |
| D49 | Extra volumes are btrfs by default; XFS stays an option | XFS can't shrink, so `pd move` to a smaller disk would leave XFS volumes bigger than their pool; btrfs shrinks, and `chattr +C` covers files rewritten in place (§2.3) | agreed |
| D50 | Clear the decoy's old-data traces above the floor: `pd compact` (decoy) and `init-hidden` on a decoy from an earlier boot rewrite the pool metadata over random data and refill free blocks above the boundary through a throwaway key; `pd compact` on the decoy requires `--no-hidden` or `--hidden-offset`; the frontier monitor warns when decoy data is above the floor | Freed blocks keep old data and the CoW metadata keeps old pointers to them; hidden data over them would be visible (§10.5) | agreed |
| D51 | Setup's LVM settings go into a private `lvm.conf` (`LVM_SYSTEM_DIR`), not `--config`/`--devices` on the command line; no `-qq`; human sizes | LVM records each full command line in the on-disk metadata (§6.1) | agreed |
| D52 | `pd rekey` on a FIDO2 side generates a new boot-volume key | Otherwise an old `fido2.txt` with the old password still opens the side | superseded by D64 |
| D53 | `pd randomize` checks that every byte was written | `dd` exits non-zero at the end of the disk either way, so a failed fill went unnoticed; unfilled space would stand out | agreed |
| D54 | `pd compact` trims at `/mnt`; stock-boot mounts the hidden boot volume `ro,rescue=nologreplay` | ext4 records the last mount point; invariant 3 for the printed commands too | agreed |
| D55 | The salt becomes the 32 bytes just below 1 MiB on the disk (as hex); the boot image carries none, and an explicit salt can still come from a file next to it. Reverses D22 and the USB salt | A boot image identical for every install: downloadable, reproducible, replaceable. The salt can't be lost with the USB, and rebuilding the USB needs no config (§4.2) | agreed |
| D56 | The disk's first and last MiB hold random data instead of zeros, still unused; setup refills either if it matches any partition-table or filesystem signature, checking a superset of what the boot checks | Zeros at both ends are a fixed pattern; the buffers' job doesn't need zeros (§3, §4.2) | agreed |
| D57 | The boot tries disks in order: never those too small or whose salt bytes are all equal, partitioned-looking ones last, largest first; one Argon2 run (and FIDO2 touch) per disk tried | Each disk has its own salt; with one pd disk the right password still costs one key derivation (§7.2) | agreed |
| D58 | The release image has fixed Argon2 parameters; keymap, other Argon2 parameters and an explicit salt are optional files in `BOOTX64.EFI.extra.d/`, which systemd-stub passes to the initrd | The image itself stays identical for everyone (§7.1) | agreed |
| D59 | No seed or fill key is kept to prove the disk's edges are filler | The free space can never be proven unused, so proving only the header invites the question why, and the fill key would expose the hidden side (§2.4) | agreed |
| D60 | Prebuilt releases from GitHub Actions: every published file is reproducible and rebuild-checked; releases build without any binary cache but cache.nixos.org, carry `SHA256SUMS` and a build-provenance attestation, and are tested first; third-party actions are pinned to commit SHAs | Users download instead of building; anyone can check a download against the source (release.md) | agreed |
| D61 | The frontier monitor also scans the decoy's pool metadata for stale btree leaves (`tmeta_scan`) and warns if they point above the floor or into the hidden side's data | The decoy can rise above the floor and fall back between hidden boots; its stale metadata then points at old data the hidden side would overwrite (§10.5) | agreed |
| D62 | The release's `pd-live` bundle boots a pd disk from a live system: the boot USB's `early.sh`, disk check and static tools, run by `boot.sh` with the per-install files as options | A lost or unavailable USB doesn't lock the user out, and booting needs nothing typed but the password and options ([booting.md](booting.md#from-a-live-system)) | agreed |
| D63 | FIDO2 has two modes (`fido2_mode`): "data" (default) wraps the data key in the boot volume, which stays keyed by the password; "boot" wraps the boot volume's key, with the slots on the USB | Data mode keeps the USB generic and the boot to one touch, but lets a password be checked offline; boot mode protects a side's existence with the security key too (§4.1) | superseded by D64 |
| D64 | FIDO2 always protects the data key, in the boot volume; the boot-key mode with slots on the USB is gone | A per-install file on the USB proves an install exists, which outweighs making password guesses need the key; one mode is less to maintain (§4.1) | agreed |

---

## 14. Open questions

None at the moment; what's left is in [roadmap.md](roadmap.md).
