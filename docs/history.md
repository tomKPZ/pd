# History

This is reconstructed from the git history. Most commit messages are terse, so the diffs carry most of
the information. Every decision was made by hand; none of it is accidental.

## June 2024: proving the trick
* **`create_disk.sh` prototype.** A 1 GiB image was filled with a recognizable pattern, then built up:
  MBR partition → LUKS → LVM thin pool → filesystem. A `pypy3` "disk visualizer" printed which 1 MiB
  chunks had changed, as a colored grid.
* **Stress tests** (write and delete 128 MiB random files) established that dm-thin allocates
  sequentially from the front ("ensure junk is at the end", "stress test").
* **"LVM tricks", "reverse".** A dm-linear table reversing the device in 8 MiB chunks made a second pool
  grow from the back.
* **Python port (`create_disk.py`).** It stacked both directions on one image ("write from both ends")
  and briefly tried dm-integrity.
* **"rebuild vg to handle discards".** The write/delete loop gained `fstrim` and `vgchange -an/-ay`.
  These experiments led to the rule that discards are banned: with discards, the allocation cursor walks
  forward through freed space instead of the filesystem reusing its own blocks
  ([design.md §2.3](design.md#23-no-discards-ever-the-central-safety-invariant)).

## July 2024: the boot chain
* **A separate repository for a hand-made initrd and custom kernel** (`init`, `bootstrap.sh`,
  `build.sh`, a kernel `.config`): busybox, static binaries, argon2, a detached LUKS header, `poweroff`
  on exit. Started 26 June, merged on 2 July.
* **`pd.py` (from 29 June).** Disk setup as a stack of context managers (`cascade`), a btrfs header
  volume, LUKS, and LVM experiments (`thin_metadata_size`, `pvmove`, swap).
* **2 July:** "separate early and late loading", "basic kexec works", "modify initramfs" (appending a
  cpio to the target initrd).
* **3–5 July:** reversed volume, then "decoy and hidden work". The hidden boot became a `sed` on the
  table offset, plus a hidden salt suffix.
* **4 July: "move kexec and lvm out of bootstrap initrd".** lvm, kexec, and the thin tools moved into
  the plain volume and were symlinked into place by `late.sh`. The USB then held only what it took to
  open one encrypted volume.
* **6–7 July:** real passwords, Argon2 parameters, a random LUKS volume key, entropy from gpg mixed
  with `/dev/random`, disk size no longer hardcoded, a reversed map that works when the disk size isn't a
  multiple of the chunk size, and the first real install.
* **16–20 July: static tools.** busybox, cryptsetup, lvm2, kexec-tools, argon2, json-c, libaio,
  util-linux, and thin-provisioning-tools were built from git submodules with clang + LTO (musl where
  possible). This lived in a third repository, merged on 20 July.
* **25 July: dm-integrity replaced** by btrfs `--data=dup` with strong checksums in the header volume.
* **26–28 July:** btrfs subvolumes, optional shred, separate decoy and hidden init, optional swap, a
  pathlib refactor, Arch installation (`pacstrap`, mkinitcpio hook), and the same VG name on both sides.

## August–September 2024: tuning for real installs
* **"tune some params", "final tune params"** (3 August): 512 KiB thin chunks with 128 MiB metadata
  (close to what stock LVM picks for a 1 TB pool), a 256 MiB reverse chunk (briefly 1 GiB), and
  Argon2id t=14, m=256 MiB, p=8.
* **Hibernation disabled** (4 August). An rdseed entropy source was added (removed later).
* **Additions:** snapshot subvolumes, the microcode hook, the `data` LV (ext4), `noatime`.

## January–February 2025: dracut, NixOS, menus
* **1 January: the static tools were removed** because of the maintenance burden. `create_disk.py` was
  removed too. Switching the Arch target from mkinitcpio to dracut was tried, then reverted on 25 January
  because it added nothing.
* **19 January: "dracut for UKI".** The boot environment became a dracut initrd built from the host
  kernel, entered through `rd.break=pre-mount` and an overridden `dracut-emergency`, with `ukify`
  producing the UKI.
  * "stable disk ID": `early.sh` iterates `/dev/disk/by-id` and hands the target a by-id symlink.
  * The fake test systems were removed ("remove non-arch install codepath").
* **20–26 January: NixOS.** NixOS was added, and Arch and NixOS were installed side by side in one VG.
* **2–17 February:**
  * the `@boot` subvolume;
  * the `BootCurrent` NVRAM entry deleted at boot;
  * config knobs (`STATIC_SALT`) so the UKI can be rebuilt for an existing disk;
  * `detect_shift`, a Shift-held system menu (`fzf`), and a NixOS generation menu.

  The repository was left mid-refactor. The working versions lived only on deployed plain volumes.

## October 2026
* **"sync changes":** the working `late.sh` and `systems/` from a deployed decoy plain volume, plus a
  NixOS unlock hook migrated from `preLVMCommands` to a systemd initrd service (`patches/`).
* **A design review produced the v2 design** ([design.md](design.md), decision log in §13).
* **v2 was implemented on branch `v2` the same month**, test-first in QEMU. Things learned along the
  way are in the decision log (D26–D35), notably:
  * dm-thin can't run in a read-only table, so the boot environment maps thin LVs from `thin_dump`
    output instead of activating the pool;
  * a plain `ro` btrfs mount on a writable device still replays a dirty log, so v1's boot environment
    writes to the disk after a crash;
  * without discards, ext4 touches a whole thin volume and btrfs creeps slowly; a single `fstrim` at
    shutdown (which v1 installs already used) keeps both near their live data;
  * stock `lvcreate` puts the thin pool's `tmeta` at the end of the PV, which decides where the hidden
    side goes.
