# Notes for coding agents

pd builds a plausible-deniability boot setup: one disk holding a decoy system and a hidden system, built
from stock Linux pieces (dm-crypt, dm-thin, LVM, btrfs, a UKI that `kexec`s the chosen OS). Read in this
order: [README.md](README.md) → [docs/design.md](docs/design.md) →
[docs/threat-model.md](docs/threat-model.md). Then read [docs/v1.md](docs/v1.md) if you're touching the
current code, or [docs/roadmap.md](docs/roadmap.md) for what's being built.

## State of the repo

* `main` holds v1. It works and is deployed. Its files are described in [docs/v1.md](docs/v1.md).
* `v2` is the rewrite in progress. The target design is [docs/design.md](docs/design.md); progress is
  tracked in [docs/roadmap.md](docs/roadmap.md).
* Deployed v1 systems depend on scripts stored on their disk, not in the repo, so changing repo files
  doesn't break them. Migrating it is a separate, deliberate step
  ([docs/migration.md](docs/migration.md)).

## Invariants: never break these

1. **Nothing on the USB or in the decoy refers to the hidden side.** No hidden-specific code paths,
   strings, files, or comments in anything that ends up on the USB or in the decoy's plain volume or OS.
   The only hidden-specific value is the offset in the user's head.
2. **No discards at any layer.** Thin pools use `--discards=ignore`, filesystems are mounted
   `nodiscard`, dm-crypt runs without `allow_discards`. Discards break both deniability and the bounded
   growth that keeps the two sides apart
   ([design.md §2.3](docs/design.md#23-no-discards-ever-the-central-safety-invariant)).
3. **The boot environment never writes to the internal disk.** Mappings are read-only; btrfs is mounted
   `ro,rescue=nologreplay`.
4. **Keys never reach persistent storage.** No temporary files for key material, and never print keys
   or salts in logs.
5. **The decoy uses only stock, ordinary commands and names,** because LVM metadata records them
   ([design.md §6.1](docs/design.md#61-decoy-stock-lvm)).
6. **The hidden side's pool metadata sits at the front of the hidden PV,** and the hidden region never
   covers [0, 256 MiB) or the decoy's pool metadata. (Migrated v1 installs keep their v1 tables; see
   [docs/migration.md](docs/migration.md).)
7. **Stock kernel features only.** No custom modules.

## Rules for working here

* **Never run setup, device-mapper, LVM, or cryptsetup commands on the host, and never touch real block
  devices.** Everything runs inside QEMU against image files, as an ordinary user. KVM is available
  (`/dev/kvm`, group `kvm`); OVMF is at `/usr/share/OVMF/`.
* **Nix:** pass `--extra-experimental-features 'nix-command flakes'` and `-j auto`. The Nix build sandbox
  has no KVM, so Nix only builds artifacts. Run QEMU tests outside Nix builds.
* **Any design change** updates the decision log in [docs/design.md §13](docs/design.md#13-decision-log).
  Update milestone status in [docs/roadmap.md](docs/roadmap.md).
* **Style:** 4-space indent ([.editorconfig](.editorconfig)). Shell scripts use `set -euo pipefail`.
  Python follows `pd.py`: resources as `contextlib.contextmanager`s, external commands through a `cmd()`
  helper that records a trace.
* **Git:** never push. Commit only when asked.
