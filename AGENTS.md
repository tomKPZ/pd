# Notes for coding agents

pd builds a plausible-deniability boot setup: one disk holding a decoy system and a hidden system, built
from stock Linux pieces (dm-crypt, dm-thin, LVM, btrfs, a UKI that `kexec`s the chosen OS). Read in this
order: [README.md](README.md) → [docs/design.md](docs/design.md) →
[docs/threat-model.md](docs/threat-model.md) → [docs/install.md](docs/install.md). Read
[docs/v1.md](docs/v1.md) before touching anything that deployed v1 installs depend on, and
[docs/roadmap.md](docs/roadmap.md) for what's left.

## State of the repo

* `main` holds v1. It works and is deployed. Its files are described in [docs/v1.md](docs/v1.md).
* `v2` holds the rewrite: complete and tested in QEMU, not yet used on real hardware. Its design is
  [docs/design.md](docs/design.md); remaining ideas are in [docs/roadmap.md](docs/roadmap.md).
* Deployed v1 systems depend on scripts stored on their disk, not in the repo, so changing repo files
  doesn't break them. Migrating it is a separate, deliberate step
  ([docs/migration.md](docs/migration.md)).

## Invariants: never break these

1. **Nothing on the USB or in the decoy refers to the hidden side.** No hidden-specific code paths,
   strings, files, or comments in anything that ends up on the USB or in the decoy's plain volume or OS.
   The only hidden-specific value is the offset the user remembers (computable from the decoy's layout,
   so not secret; it just mustn't be written down anywhere pd puts files).
2. **No discard reaches the disk, and the pool sees discards only at shutdown.** dm-crypt runs
   without `allow_discards`; filesystems are mounted `nodiscard`; pools use `--discards=nopassdown`;
   the only trim is one `fstrim` at shutdown. Discards on the disk break deniability, and discards
   during a session break the bounded growth that keeps the two sides apart
   ([design.md §2.3](docs/design.md#23-discards-never-to-the-disk-and-to-the-pool-only-at-shutdown)).
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
* **Nix:** pass `--extra-experimental-features 'nix-command flakes'` and `-j auto`, and use `path:`
  flake references (`nix build "path:$PWD#testenv"`): Nix's libgit2 can't read this repo's reftable
  refs, and `path:` also picks up uncommitted files. The Nix build sandbox has no KVM, so Nix only builds
  artifacts. Run QEMU tests outside Nix builds.
* **Tests:** `python3 -m unittest discover -s tests -t .` builds `.testenv` and runs everything (about
  15 minutes; `test_churn` is the slowest). Useful variables: `PD_SKIP_NIX_BUILD=1` (reuse `.testenv`),
  `PD_TEST_DIR=/tmp/x PD_KEEP_TEST_DIR=1` (keep disk images and serial logs). `tests/debug_boot.py
  WORKDIR` boots a kept disk into the recovery shell and runs commands from stdin.
* **Layout:** `boot/` (USB initrd scripts), `plain/` (boot-volume scripts; `plain/hidden/` is installed
  only on the hidden side), `target/` (hook, NixOS module, Arch hook and trim unit for installed OSes),
  `pd/` (setup CLI), `nix/` (static tools, kernel modules, boot initrd, tool VM, setup UKI, fake target,
  NixOS test system), `tests/`, `tools/` (`detect_shift.c`), `v1/` (the v1 code, for reference).
* **Opt-in tests:** `PD_TEST_ARCH=1` (Arch install, downloads packages into `~/.cache/pd-tests`) and
  `PD_TEST_NET=1` (setup environment networking) need internet access.
* **Any design change** updates the decision log in [docs/design.md §13](docs/design.md#13-decision-log).
  Update milestone status in [docs/roadmap.md](docs/roadmap.md).
* **Style:** 4-space indent ([.editorconfig](.editorconfig)). Shell scripts use `set -euo pipefail`.
  Python follows `pd.py`: resources as `contextlib.contextmanager`s, external commands through a `cmd()`
  helper that records a trace.
* **Git:** never push. Commit only when asked.
