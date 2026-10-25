# Migrating a v1 install to v2 (draft)

> [!CAUTION]
> Draft. Nothing here has been tested yet. The migration tool and its tests are milestone M9 in
> [roadmap.md](roadmap.md). Don't run any of this on a real disk until the tests pass.

## Why no data re-encryption is needed

A v1 data region is LUKS2 with a detached header and data offset 0: aes-xts-plain64, a 512-bit random
volume key, 4096-byte sectors. The dm-crypt mapping cryptsetup builds for it should be identical to a
plain-mode mapping with the same key, cipher, and sector size at IV offset 0 *(verify in M5)*. If so,
the LUKS volume key *is* v2's `data.key`, and the data regions stay exactly as they are.

What does change:
* **Both plain volumes are re-keyed.** v2 uses a 64-byte Argon2 output and no hidden salt suffix, so
  both plain keys change. Take the opportunity to use a new salt.
* **New contents in each plain volume:** `data.key` (the extracted volume key), `crypt.env`, the v2
  `late.sh`, and `bin/` with static tools. `header.dat` goes away.
* **New hook in every target OS**, plus a new USB.

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

## Steps (planned)

1. **Install a transitional hook in every OS on both sides.** It follows the v1 path when
   `/boot_tmp/header.dat` exists and the v2 path otherwise. Rebuild the initrds and confirm each OS still
   boots with the v1 USB.
2. **Boot a throwaway environment** (live ISO + pd bundle). Then, per side:
   1. Derive the v1 key: 512-byte Argon2 output, with the `a` suffix for the hidden side. Open the plain
      volume read-only and copy its contents to tmpfs.
   2. Extract the volume key: `cryptsetup luksDump --dump-volume-key --header header.dat
      --key-file key.bin --keyfile-offset 64 <data mapping>`. Write the 64 raw bytes to `data.key`.
   3. Verify that a plain-mode mapping with `data.key` shows the same plaintext as the LUKS mapping:
      compare the LVM label and the first GiB.
   4. Re-create the plain volume at the same location under the v2 key (new salt), and write the v2
      contents. Keep `table.txt`.
   5. Hidden side only: add the decoy's `data.key` and table, for the frontier monitor.
3. **Write the v2 USB** with the new salt. Boot both sides.
4. **Remove the transitional v1 branch** from the hooks.

Rollback: keep the v1 USB until both sides boot with v2. Keep copies of the old plain volumes only in
RAM during the migration. A 255 MiB random-looking file stored elsewhere is itself evidence.

