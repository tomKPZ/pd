# Plain Disguise: plausible deniability for a whole Linux system

> **Plain Disguise** (`pd`): the hidden system hides in plain sight, in what looks like the decoy's free
> space, inside headerless *plain* dm-crypt volumes.

pd sets up one disk with two complete, independent Linux installations:

* a **decoy** system that looks like an ordinary, slightly paranoid full-disk-encryption setup, and
* a **hidden** system whose existence can't be shown, even by someone who holds the boot USB and
  the decoy password.

It uses only stock kernel features: dm-crypt, device-mapper, LVM thin provisioning, and btrfs. There
are no custom kernel modules, and the decoy uses no "hidden volume" feature for an examiner to notice.

## Why not VeraCrypt?

VeraCrypt's hidden volumes work, but they are VeraCrypt's headline feature, so using VeraCrypt invites
the question "is there a hidden volume?". pd's decoy is built from ordinary, unremarkable pieces. The
goal is to avoid that question altogether.

## How it works

```
 0   1 MiB           256 MiB                                                                   end
 |000| decoy plain  | decoy data: grows →        (random-looking free space)   ← grows: hidden data | hidden plain | decoy pool metadata |
```

* The whole disk is first filled with random data. Discards (TRIM) never reach the disk, so regions
  that hold data look the same as regions that never did.
* **dm-thin allocates physical blocks sequentially.** The decoy's thin pool sits on the disk normally,
  so it fills from the front. The hidden system's thin pool sits on a *reversed* view of the disk
  (built with a dm-linear table), so it fills from the back. Each side looks like an encrypted volume
  that is mostly free space.
* Each side has a small encrypted **plain volume**. It's keyed by Argon2id(password, salt) and found by
  trying that key at a fixed offset. It holds the side's data-region key, boot scripts, and tools. The
  boot USB only knows the decoy's offset. Optionally, FIDO2 security keys (e.g. YubiKeys) become a
  second factor, so passwords can't be guessed offline.
* **Booting the decoy:** boot the USB and type the decoy password.
* **Booting the hidden system:** press Enter at the password prompt to fall into the "recovery" shell,
  change one number with a one-line `sed`, re-run the boot script, and type the hidden password.
  Nothing on the USB refers to the hidden system; you remember the number. (It isn't a secret: what hides
  the hidden system is that, without its password, it's indistinguishable from the random fill.)
* The boot USB runs a small Linux environment that unlocks the chosen side and `kexec`s into that
  side's own kernel. The decoy and hidden systems are separated by a full OS boundary, so shell
  history, caches, and logs on one side never contain anything from the other.

The details are in [docs/design.md](docs/design.md).

## Status

| | |
|---|---|
| **v1** (the current code: `pd.py`, `early.sh`, `late.sh`, …) | Works, including dual-booting Arch and NixOS on one side. Needs root, has hard-coded config, and builds with the host's dracut and kernel. Documented as-built in [docs/v1.md](docs/v1.md). |
| **v2** (branch `v2`) | Complete and tested end to end in QEMU; not yet used on real hardware. Nix-pinned static build, minimal boot USB, LUKS dropped, read-only boot environment, frontier monitor, RAM-only setup environment, NixOS module and Arch hook, migration from v1, rootless QEMU test suite. See [docs/design.md](docs/design.md) and [docs/roadmap.md](docs/roadmap.md). |

> [!WARNING]
> This is a research project. **The repository is public**, so its boot scripts are recognizable. An
> adversary who knows about pd can identify a pd boot USB and will assume a hidden system exists. See
> [docs/threat-model.md](docs/threat-model.md) for what pd does and doesn't protect against.

## Documentation

| Doc | Contents |
|---|---|
| [docs/design.md](docs/design.md) | v2 design, the reasoning behind every decision, decision log |
| [docs/install.md](docs/install.md) | Building, configuring, setting up a disk, installing Arch and NixOS |
| [docs/threat-model.md](docs/threat-model.md) | Adversaries, guarantees, what is out of scope, operational rules |
| [docs/booting.md](docs/booting.md) | Decoy boot, hidden boot, boot menus, recovery |
| [docs/stock-boot.md](docs/stock-boot.md) | Without pd's USB: a stock decoy, and the hidden side booted from a live system |
| [docs/v1.md](docs/v1.md) | Exact as-built description of v1 (what deployed installs run) |
| [docs/migration.md](docs/migration.md) | Moving a v1 install to v2 |
| [docs/roadmap.md](docs/roadmap.md) | Milestones and ideas |
| [docs/history.md](docs/history.md) | How the design evolved, reconstructed from the git history |
| [AGENTS.md](AGENTS.md) | Orientation and rules for coding agents working on this repo |

## Quick start

[docs/install.md](docs/install.md) has the full procedure. In short:

1. Build: `nix build "path:$PWD#setupUki" --out-link setup.efi` (RAM-only setup environment) and
   `nix build "path:$PWD#testenv" --out-link env` (boot environment pieces).
2. Write a `pd.toml` (disk, salt from `pd salt`, systems, optional volumes).
3. Build the boot USB: `python3 -m pd usb --config pd.toml --env env --output BOOTX64.EFI`.
4. Boot the target machine from the setup UKI and run `pd randomize`, `pd init-decoy --run` (install the
   decoy OSes), and `pd init-hidden --run` (install the hidden OSes; memorize the printed offset).
5. Boot from the boot USB ([docs/booting.md](docs/booting.md)).

Tests: `python3 -m unittest discover -s tests -t .` (about 15 minutes, needs KVM; see
[AGENTS.md](AGENTS.md)). v1 (branch `main`) is configured by editing the constants at the top of its `pd.py` and must be
run as root ([docs/v1.md](docs/v1.md)).
