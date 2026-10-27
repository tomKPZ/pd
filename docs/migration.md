# Migrating a v1 install to v2

This also covers moving an earlier v2 install's salt from the USB to the disk
([below](#moving-the-salt-to-the-disk)).

> [!CAUTION]
> `pd migrate-v1` is tested only against v1-format disks built in QEMU (`tests/make_v1.py`,
> `tests/test_migrate.py`). It rewrites both boot volumes in place. Back up nothing to unencrypted
> storage; rehearse on a disk image first.

## Why no data re-encryption is needed

A v1 data region is LUKS2 with a detached header and data offset 0: aes-xts-plain64, a 512-bit random
volume key, 4096-byte sectors. The dm-crypt mapping cryptsetup builds for it should be identical to a
plain-mode mapping with the same key, cipher, and sector size at IV offset 0 *(verify in M5)*. If so,
the LUKS volume key *is* v2's `data.key`, and the data regions stay exactly as they are.

What does change:
* **Both plain volumes are re-keyed.** v2 uses a 64-byte Argon2 output and no hidden salt suffix, so
  both plain keys change. The salt moves to the disk: v1's first MiB is zeros and unused, and becomes
  the random header holding the salt (design.md §4.2).
* **New contents in each plain volume:** `data.key` (the extracted volume key), `crypt.env`, the v2
  `late.sh`, and `bin/` with static tools. `header.dat` goes away.
* **New hook in every target OS**, plus a new USB: the release boot image.

## What stays v1-shaped

* **The data-region tables.** The v1 hidden table maps [0, D − 256 MiB). The v2 rule would make it one
  chunk shorter, and the hidden PV can't shrink because its thin pool covers the last extents. Migrated
  installs keep their existing `table.txt` files; v2's `late.sh` accepts any table.
* **The decoy's LVM layout and history.** Pool metadata stays at the front (1-extent trick), and the
  metadata history records pd's commands. So H = D − 1 MiB and the hidden plain volume stays at
  [D − 256 MiB, D − 1 MiB).

> [!WARNING]
> **Never re-create the decoy of a migrated install with stock LVM placement.** Stock placement puts
> the decoy pool's metadata at the end of the disk, on top of the hidden plain volume. pd can't detect a
> hidden side by design, so re-initializing a decoy must require the hidden offset or password and check
> for overlap before writing anything (roadmap).

## Steps

1. **Install a transitional hook in every OS on both sides** that accepts both formats: v1 hands over
   `/boot_tmp/header.dat`, v2 hands over `/boot_tmp/data.key`.
   * NixOS: import `target/nixos/pd.nix` with `pd.enable = true; pd.v1Compat = true;` and **remove** the
     system's own `pd-unlock` unit (both would run, and v1's `shred -u /boot_tmp/*` would destroy v2's
     keys).
   * Arch: guard the body of v1's `mycrypt` hook with `if [ -e /boot_tmp/header.dat ]; then … fi`, and
     add the v2 `pd` hook after it.

   Rebuild the initrds and confirm each OS still boots with the v1 USB.
2. **Boot a throwaway environment** with pd, write a v2 config (without `salt`), and run:
   ```sh
   pd migrate-v1 --config pd.toml --v1-salt <v1 salt> --v1-hidden-offset <v1 hidden sector>
   ```
   Omit `--v1-hidden-offset` if there is no hidden side. For each side, the tool:
   1. derives the v1 key (512-byte Argon2 output, salt + `a` for the hidden side) and reads the v1 boot
      volume read-only;
   2. extracts the LUKS volume key (`cryptsetup luksDump --dump-volume-key`, in `/run` only);
   3. checks that plain dm-crypt with that key shows the LVM PV;
   4. only after both sides pass, checks that the first MiB is still all zeros and that the hidden side
      has nothing in the 256 MiB chunk at the start of the disk (v1's hidden region starts at 0; its
      pool reaches that chunk last), and writes the random header. The trailer is left alone: v1's decoy
      region reaches the end of the disk. An interrupted run keeps the header it wrote;
   5. re-creates each boot volume in place under the v2 key (with the disk's salt), keeping
      `table.txt` and `systems/*/env.sh`, replacing `extra.sh` with the v2 versions, and adding the
      frontier monitor to the hidden side.
3. **Write the v2 USB** (`pd usb`, [install.md §3](install.md#3-the-boot-usb)). Boot both sides.
4. **Remove the v1 compatibility** (`pd.v1Compat`, or the guarded `mycrypt` hook).

Rollback: keep the v1 USB until both sides boot with v2. The data regions are never modified, but the
v1 boot volumes are overwritten in step 2, so the v1 USB stops working after it.

## Adding a hidden side afterwards

A migrated install without a hidden side can get one, like any v2 decoy. If the decoy's pool was ever
nearly full, its allocations are spread over the whole disk, and `pd init-hidden` refuses until they're
moved down:
```sh
pd compact --config pd.toml --no-hidden   # decoy password; trims, moves data below the hidden side's floor,
                                          # and refills the space above with random data
pd init-hidden --config pd.toml --run
```
The refill matters: the decoy's freed blocks still hold its old data, and its pool metadata still
points at them, so hidden data written over them would show ([design.md
§10.5](design.md#105-traces-of-the-decoys-old-data)). `pd init-hidden` does the same cleanup on any
decoy from an earlier session, so even without compacting, it takes a while on a large disk.
`pd compact` needs the decoy's filesystems to be btrfs, XFS or ext4 (it mounts them to trim) and LVM's
spare metadata LV, which v1's pool has. *(tested: `test_compact` builds a v1 decoy that was nearly full,
migrates it, compacts it, adds a hidden side and boots both.)*

## Moving the salt to the disk

Installs set up (or migrated) before the salt moved to the disk keep it in the config and on the USB
(`salt.cred`, or `salt.txt` inside an old boot image), and their first MiB is zeros. They keep working
that way. To move the salt to the disk, from the setup environment:
```sh
pd header --config pd.toml --hidden-offset N    # or --no-hidden; asks for both passwords
```
It reads the old salt from the config (or `--old-salt S`, or `--old-salt-from OLD.EFI` to take it from
an old boot image), checks that the first MiB is all zeros and that both sides open, saves the old salt
next to the config (`pd.toml.old-salt`), writes the random header (and the trailer, where the layout
leaves it unused), then re-keys each side to the new salt, the same way `pd rekey` does. Interrupted,
run it again: each side opens with one of the two salts, and it continues. Then remove `salt` from the
config and write the boot USB again (`pd usb`): it's just the release image now, plus any FIDO2 or
keymap files. *(tested: `test_migrate` migrates a v1 disk with an explicit salt, moves it to the disk,
and boots both sides.)*

As with any re-key, an interruption *while* a boot volume is being rewritten loses that boot volume,
since its only complete copy is in RAM.
