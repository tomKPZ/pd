# pd design (v2)

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
* **No unencrypted data on disk** except the zeroed first MiB. Someone who finds the disk sees random
  data and no partition table: a drive that looks wiped or never provisioned.
* **The first MiB is zeroed**, for two reasons:
  * minor: the disk can't be misidentified as some partition table or filesystem by chance;
  * main: the decoy's plain volume then starts at sector 2048, the standard first-partition offset.
    2048 is the one number the hidden boot replaces, with a single hand-typed `sed`.
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

Measured (`tests/test_churn.py`: 8 GiB volume; each cycle fills to 25%, deletes half at random, and
writes about 1 GiB; "high-water" is the highest allocated pool block, which is what decides a
collision):

| Filesystem | No trim | `fstrim` + reactivation every 5 cycles |
|---|---|---|
| btrfs | 5.7 GiB after 60 cycles, 6.5 GiB (80% of the volume) after 200, still creeping | 4.2 GiB after the first session, settling at 3.6–3.8 GiB, below peak usage (~4 GiB) |
| ext4 | 7.7 GiB (the whole volume) within 15 cycles | 4.8–5.0 GiB after the first session, then flat |

(Ranges are from separate runs.)

* **btrfs** allocates data block groups first-fit from low device offsets and deletes empty block
  groups; with the shutdown trim its footprint tracks live data. It's the default, and root volumes
  must be btrfs (the boot path mounts their `@boot` and `@nix` subvolumes).
* **ext4** deliberately spreads allocations and metadata across block groups (the Orlov allocator).
  With the shutdown trim it's bounded by live data plus each session's new writes, like btrfs; the
  ~5 GiB above comes from writing about 5 GiB in a single session on an 8 GiB volume. Its metadata
  stays wherever the first session put it. ext4 is a reasonable choice for data volumes holding large
  files that are rewritten in place: an in-place overwrite reuses mapped blocks, while btrfs
  copy-on-write allocates new ones (`chattr +C` gives btrfs the same behavior for chosen directories,
  at the cost of checksums and compression). Without the shutdown trim, ext4 reaches the whole volume,
  so it depends on the trim more than btrfs does.
* **Without the shutdown trim** (or if it's skipped, e.g. after a crash), footprints creep as in the
  first column. The hidden side's thin LVs are capped (§6.2), so it can't grow past its cap either way;
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
which is indistinguishable from the fill.

(v1 generated this key in Python and printed it at the end, for debugging. Anyone holding that key
could map out every block written since setup.)

---

## 3. Disk layout

`D` is the disk size rounded down to 1 MiB. All offsets are physical.

```
0      1 MiB         256 MiB                                                  H-255 MiB      H                  D
|zeros | decoy plain | PV hdr, swap, decoy thin data →     ...     ← hidden thin data | hidden plain | decoy tmeta/pmspare |
                     |<--------------------------- decoy data region [256 MiB, D) ------------------------------------->|
                     |<--------- hidden data region [256 MiB, H-255 MiB), reversed --------->|
```

| Region | Range | Contents |
|---|---|---|
| zero header | [0, 1 MiB) | zeros |
| decoy plain volume | [1 MiB, 256 MiB) | plain dm-crypt (Argon2 key), btrfs |
| decoy data region | [256 MiB, D) | plain dm-crypt (decoy `data.key`), LVM PV |
| hidden data region | [256 MiB, H − 255 MiB), reversed in 256 MiB chunks | plain dm-crypt (hidden `data.key`), LVM PV |
| hidden plain volume | [H − 255 MiB, H) | plain dm-crypt (Argon2 key), btrfs |

* **The decoy data region spans the rest of the disk**, because an ordinary install would. The hidden
  regions lie inside it, in space the decoy's pool hasn't allocated.
* **What H is.** The decoy's LVM layout is stock (§6.1). Measured with LVM 2.03.41, stock placement
  is `[swap][pmspare][pool data][tmeta]`: the spare metadata LV goes in front of the pool's data and
  `tmeta` goes at the very end of the PV. The decoy writes to `tmeta`, so the hidden side must stay below
  it. **H** is the physical start of the decoy metadata that lies after the pool's data. Setup reads it
  back from the decoy's LVM metadata after creating the decoy, rather than assuming it. If there's none
  (all metadata at the front), H = D − 1 MiB.
* **Overlap check.** Before writing anything, hidden setup checks that no decoy LV other than the
  pool's data lies between the start of the pool's main data segment and the top of the hidden boot
  volume.
* **Both plain volumes are 255 MiB (522240 sectors)**, so they differ only in their start sector: 2048
  for the decoy, (H − 255 MiB)/512 for the hidden side. That is what makes the hidden boot a one-number
  `sed`.
* **The hidden data region never covers the decoy's plain volume or the zero header.** Even if the
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
| plain key | Argon2id(password, salt), 64 bytes. Defaults: t=14, m=256 MiB, p=8, v1.3 | the side's plain volume |
| `data.key` | 64 random bytes generated at setup, stored in the plain volume | the side's data region |

* **Cipher.** Everything uses plain dm-crypt (aes-xts-plain64, 512-bit keys, 4096-byte sectors), so
  there's no header anywhere on disk to find.
* **Salt.** A random 64-byte salt is stored on the boot USB, and both sides use it. Keeping the salt off
  the disk means the boot script derives one key and tries it on every disk, with no per-disk lookup and
  no alignment issues. The cost is that a USB belongs to one machine.
* **Passwords must differ.** Setup enforces this, and checks that the two passwords don't derive the
  same key. (v1 instead appended a fixed suffix `a` to the salt for the hidden side. The
  suffix is public, so it added nothing.)
* **Changing a password** (`pd rekey`) re-creates that side's 255 MiB plain volume under the new key
  with the same contents (held in RAM, then compared after writing). `data.key` and the data region
  stay as they are. `pd rekey --new-salt` changes the salt the same way; both sides and the USB then
  need it.
* **Why not LUKS.** v1 kept a detached LUKS2 header in the plain volume, using Argon2 output bytes
  64–511 as the keyslot passphrase. The motivation was reencryption and FIDO2, but neither was ever used.
  A coerced examiner walks the whole boot chain, so every layer has to be explainable, and a raw key file
  gives the same separation of password from data key. Dropping LUKS also removes the odd keyslot with a
  minimal PBKDF. If FIDO2 is ever wanted, it has to be mixed into the Argon2 step anyway, because the
  plain volume opens with the password alone.
* **The hidden plain volume also stores the decoy's `data.key`,** so the hidden side can watch the
  decoy (§10.2).
* **Keys reach cryptsetup as files, never on stdin.** In plain mode, cryptsetup hashes a key read from
  stdin (`--key-file=-`) like a passphrase, but uses a key file's bytes directly. Setup passes keys as
  the read end of a pipe (`--key-file=/proc/self/fd/N`), so they never touch disk and match what the
  boot scripts read from tmpfs files.

---

## 5. The plain volume

The plain volume is a 255 MiB btrfs filesystem with `--data=dup` and blake2 checksums. That gives
integrity and self-healing for the most important 255 MiB on the disk without a dm-integrity layer. It
is mounted read-only during boot and never mounted by the running OSes.

```
late.sh                   stage-2 boot script
thin-ro.sh                read-only thin LV mapping (§7.3)
data.key                  this side's data-region key
table.txt                 dm table for this side's data region, with DISK standing for the disk
crypt.env                 cipher parameters handed to the target OS
bin/                      static lvm, dmsetup, pdata_tools (thin_check, thin_dump), kexec, fzf, jq, detect_shift
systems/<name>/env.sh     per-system settings: VG, root LV, kernel, initrd, cmdline
systems/<name>/extra.sh   per-system hook (e.g. the NixOS generation menu)
systems/default           symlink to the default system
hooks/frontier.sh         hidden side only: the frontier monitor (§10.2)
frontier.env              hidden side only: monitor threshold
decoy.key, decoy.table    hidden side only: used by the frontier monitor
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
* a linear `swap` LV (hibernation isn't supported);
* a thin pool over the rest of the PV;
* thin LVs: one btrfs root per installed OS. The btrfs roots have the subvolumes `@`, `@boot`, `@home`,
  `@home_snapshots`, `@nix`, `@snapshots`, `@var_cache`, `@var_lib_docker`, `@var_log`, `@var_tmp`.
* optional extra thin LVs (`[[volumes]]` in the config, btrfs or ext4), e.g. for bulk data like v1's
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
lvcreate --type thin-pool -l 100%FREE --discards nopassdown -n thinpool
lvcreate -V … -T …/thinpool -n <root>
```
No explicit extent size, chunk size, or metadata size is given; LVM picks them for the disk size. Thin
LVs get the full pool size as their virtual size. `--discards nopassdown` is the one non-default
option; it's an ordinary, documented setting (reclaim thin pool space without trimming the device).

**Why it matters: LVM metadata records its own history.** The metadata area is a ring buffer that keeps
many versions, and every version stores:
* the command that produced it (`description = "Created *after* executing '…'"`),
* `creation_host`,
* the PV's device path.

v1's decoy history shows things an ordinary install wouldn't: an explicit extent size, a one-extent pool
followed by `lvextend`, `pvmove`s, and device names like `/dev/mapper/luks_test_<uuid>`. v2's history
should look like an ordinary install. So setup also uses the device-mapper names the boot uses, and runs
under the decoy's hostname.

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
no NVRAM entry is needed. It contains:
* **Kernel:** a nixpkgs kernel with the stock config, plus a broad set of storage, input, USB, and HID
  modules loaded through modalias coldplug. A missing keyboard driver means a machine that can't boot,
  so hardware coverage matters more than size.
* **initrd** (about 10 MB): static busybox, cryptsetup, argon2, and the kernel modules (stored
  uncompressed, because busybox `modprobe` can't reliably load compressed ones), plus `/init`,
  `early.sh`, and a second, per-install cpio with `early_env.sh`, `salt.txt` and optionally
  `keymap.bmap`. GPU drivers are left
  out (their firmware is over a gigabyte); the firmware framebuffer is enough for a text console.
  `/sbin` links to `/bin`, because the kernel runs `/sbin/modprobe` to load crypto modules on demand.
* **Command line:** includes `init_on_free=1`. Pages the boot environment frees (password buffers,
  Argon2 memory) are then zeroed before `kexec` hands RAM to the next kernel.

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
new devices. It loads the configured console keymap (`keymap.bmap`, built from kbd's keymaps, with
busybox `loadkmap`), then runs `early.sh` and, if it returns, the recovery shell, in a single session
(so no console output is lost when a session leader exits).
1. Delete the `BootCurrent` NVRAM entry and remove it from `BootOrder`. This is done directly through
   efivarfs with busybox, since efibootmgr doesn't build statically; one tool fewer on the USB.
2. Prompt `Password:` with echo off (`stty -echo`). An empty password goes straight to the recovery
   shell without running Argon2: that's the start of the hidden boot.
3. Run `argon2`, writing the plain key to tmpfs.
4. For each whole disk in `/sys/block` (there's no udev):
   * `cryptsetup open --type plain --readonly --offset $PLAIN_OFFSET --size $PLAIN_SIZE …`;
   * mount btrfs with `ro,rescue=nologreplay`;
   * `exec` `late.sh` from it. Using `exec` frees early.sh's memory, password included, which
     `init_on_free=1` then zeroes.
5. If no disk works, shred the key and drop to a shell: the "recovery shell".

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
  UKI (`setupUki`: pd, LVM, cryptsetup, btrfs and ext4 tools, plus `pacstrap`, `nixos-install` and
  `pd-net` for installing the OSes). Running setup on a daily-driver machine leaves traces of **both**
  sides:
  * LVM writes copies of every VG it touches to `/etc/lvm/archive/` and `/etc/lvm/backup/`;
  * the journal records every filesystem UUID that gets mounted.

  Setup also disables LVM archive and backup explicitly.
* **Keys only travel through pipes or memory**, never temporary files.
* **Order:** randomize the disk → zero the first MiB → create the decoy → read back the decoy's layout
  to find H → create the hidden side.
* **pd's core stops at "this side's roots are mounted".** `pd init-decoy/init-hidden --run` opens a
  shell with each system's root at `$PD_ROOT/<system>`; the OS is installed with the distro's own tools
  ([install.md](install.md)).
* **Commands:** `pd salt`, `pd randomize`, `pd init-decoy`, `pd init-hidden`, `pd open`, `pd close`,
  `pd rekey`, `pd usb`, `pd install-cpio`, `pd migrate-v1` (`python3 -m pd --help`).
* **`pd open` / `pd close`** (`pd/opener.py`) work from the setup UKI or a live ISO (`pd.pyz`, a zipapp,
  with distribution tools). Read-only by default, using the boot environment's approach (read-only
  mappings, thin LVs mapped from `thin_dump`, `rescue=nologreplay` for btrfs, `noload` for ext4);
  `--rw` activates the VG. Undo steps are recorded in `/run/pd/open.json` (no keys) for `pd close`.
* **Re-initializing a decoy** is refused if the decoy password already opens a boot volume. With
  `--force`, `init-decoy` needs `--no-hidden` or `--hidden-offset` (and the hidden password); with a
  hidden side, the new decoy pool's metadata goes at the front instead of stock placement (which would
  put `tmeta` at the end, possibly on a v1 hidden boot volume), and the layout is checked against the
  hidden side before the data region is written. The disk is never re-randomized.
* **The UKI records nothing about the build host:** `pd usb` passes a neutral `--os-release`
  (ukify otherwise embeds the host's `/usr/lib/os-release`).
* **Building the USB.** `pd usb` combines the Nix-built UKI template with a small per-install cpio (salt
  and `early_env.sh`). A later rebuild, e.g. for a kernel update, reads the salt back from the existing
  USB (`pd usb --salt-from OLD.EFI`). Per-install values therefore never need to live in the repo or in `/nix/store`.
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
after someone changed the decoy's LVM layout.

### 10.3 Never clone the disk
A snapshot adversary who compares two images can see "unused" space change. Backups of hidden data must
go to another pd setup; never `dd` the disk.

---

## 11. Build and test

* **Build.** A Nix flake pins nixpkgs and builds:
  * static busybox, cryptsetup, argon2 (boot USB); static lvm2, thin-provisioning-tools, kexec-tools,
    fzf, jq, and detect_shift (boot volume);
  * the kernel and its module closure, the boot initrd, and the UKI pieces;
  * the tool VM (kernel + tiny initrd that mounts the host's `/nix/store` read-only over 9p, so tools
    aren't copied) and a fake target OS;
  * a minimal NixOS system using `target/nixos/pd.nix`, for integration tests;
  * the RAM-only setup UKI (`setupUki`).

  Build with `nix build "path:$PWD#testenv"` (and `#setupUki`). The `path:` form is needed because Nix's libgit2 can't
  read repositories using the reftable ref format.
* **Test.** Tests run QEMU (KVM, OVMF) as an ordinary user against sparse image files. Setup runs as
  root inside the tool VM; nothing needs root on the host, and no device-mapper state is created there.
  Run `python3 -m unittest discover -s tests -t .`.
* **Suite** (`tests/`):

  | Test | Covers |
  |---|---|
  | `test_layout`, `test_placement`, `test_usb`, `test_monitor_awk` | unit tests: layout arithmetic, reversed tables, hidden placement and overlap checks, salt read-back, the monitor's overlap detection |
  | `test_smoke` | the tool VM |
  | `test_e2e` | decoy boot, hidden boot via the recovery-shell `sed`, wrong password, password not echoed, read-only disk, keyboard plugged in after boot, keymap |
  | `test_safety` | boot after a crash with a dirty btrfs log; hidden cap over roots and volumes; fstrim shrinks the pool but never reaches the disk |
  | `test_growth` | on an unrandomized disk, decoy writes at the front, hidden at the back, untouched gap between |
  | `test_uefi` | the real UKI from a USB stick under OVMF; NVRAM entry and `BootOrder` cleanup; no build-host os-release |
  | `test_setup_env` | setup run from the RAM-only setup environment, then a boot; with `PD_TEST_NET=1`, its networking |
  | `test_nixos` | a real NixOS initrd with `pd.nix` unlocks the disk and mounts `/sysroot` |
  | `test_arch` | a real Arch install (pacstrap, `pd` mkinitcpio hook) boots through pd (`PD_TEST_ARCH=1`, needs network) |
  | `test_monitor` | frontier monitor report and warning; no hidden-only files in the decoy |
  | `test_churn` | btrfs and ext4 pool footprint under churn, with and without a shutdown trim |
  | `test_luks_equiv` | a v1 LUKS2 data region reads identically through plain dm-crypt with its volume key |
  | `test_migrate` | a v1-format disk migrated with `pd migrate-v1` boots both sides with v2 |
  | `test_rekey` | new passwords and a new salt on both sides; both boot with a new USB, the old password fails |
  | `test_reinit` | re-initializing the decoy is refused without `--force`; with the hidden offset, both sides still boot |
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
| Salt in a disk sector | Rejected: one key derivation can then try every disk, without per-disk salts complicating boot and alignment. |
| Hidden salt suffix | Removed: it's public, so it adds nothing. Setup enforces different passwords instead. |
| `pvmove` to tidy the pool layout (`MOVE_PES`) | Removed: cosmetic, and visible in LVM history. The decoy layout is now stock. |
| rdseed as an extra entropy source | Removed: the kernel CSPRNG is enough. |
| Secure Boot | Not planned: it ties the disk to one machine. |
| Stock decoy boot + hidden boot typed from memory in a live ISO | Possible future variant, if the USB being recognizable matters ([threat-model.md](threat-model.md)). |

---

## 13. Decision log

These decisions come from the design review held when v2 started. "Agreed" means the author confirmed
it.

| # | Decision | Why | Status |
|---|---|---|---|
| D1 | No discard reaches the disk (dm-crypt without `allow_discards`); filesystems `nodiscard`; pools `--discards=nopassdown`; one `fstrim` at shutdown | Deniability, and bounded growth (§2.3) | agreed (revised: originally `--discards=ignore`, which made a shutdown trim impossible; measurements show the shutdown trim keeps footprints near live data) |
| D2 | Hidden data reversed in 256 MiB chunks, from 256 MiB up to the hidden plain volume; fix `reversed_table`'s offset bug (it ignores `start` when `start != 0`) | The hidden side grows from the back and can't destroy the decoy's boot volume | agreed |
| D3 | Decoy data region is [256 MiB, D) | An ordinary install uses the whole disk | agreed |
| D4 | First MiB zeroed | No accidental signatures; plain volume starts at sector 2048 | agreed |
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
| D18 | A churn test decides the `data` LV's filesystem | ext4 may not reuse addresses | agreed; result in §2.3 and D30 |
| D19 | Decoy LVM is stock; the hidden side sits below the decoy's end-of-disk metadata, read back rather than assumed | Stock layout and stock history | agreed |
| D20 | Decoy LVM history looks ordinary (stock commands, boot-time names, decoy hostname); no LVM archive or backup during setup | LVM metadata records commands, host, and device names | agreed |
| D21 | Boot-environment btrfs mounts use `rescue=nologreplay` | Booting after an unclean shutdown | agreed |
| D22 | Per-install config outside the repo; salt read back from the USB (`pd usb --salt-from`) | Keeps install values out of git and `/nix/store` | agreed |
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
| D30 | Root LVs are btrfs; extra data volumes are btrfs by default and may be ext4 | With the shutdown trim both stay near live data; ext4 avoids copy-on-write reallocation for files rewritten in place, but depends on the trim more (§2.3) | agreed |
| D31 | Hidden-only features use a generic `hooks/` directory in the boot volume | Keeps hidden-specific code out of `late.sh`, which is identical on both sides | agreed |
| D32 | Kernel modules in the boot initrd are uncompressed; GPU drivers omitted | busybox modprobe can't reliably load compressed modules; GPU firmware is over 1 GB | agreed |
| D33 | Builds use `path:` flake references | Nix's libgit2 can't read reftable repositories | agreed |
| D34 | Target OSes trim once at shutdown (NixOS `pd.trimOnShutdown`, default on; Arch unit in `target/arch/`); periodic fstrim disabled | Keeps footprints near live data (§2.3) | agreed |
| D35 | A RAM-only setup UKI (`setupUki`) for running setup on real hardware | A throwaway environment with nothing to install | agreed |

---

## 14. Open questions

* Decoy footprint creep without the shutdown trim (§2.3): a documented limitation rather than capping
  the decoy's thin LVs.
* Argon2 defaults: keep t=14, m=256 MiB, p=8, or raise memory?
* Project name.
