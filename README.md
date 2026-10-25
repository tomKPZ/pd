# pd: plausible deniability for a whole Linux system

> "pd" is a working name, short for *plausible deniability*.

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

* The whole disk is first filled with random data. Discards (TRIM) are never passed down, so
  regions that hold data look the same as regions that never did.
* **dm-thin allocates physical blocks sequentially.** The decoy's thin pool sits on the disk normally,
  so it fills from the front. The hidden system's thin pool sits on a *reversed* view of the disk
  (built with a dm-linear table), so it fills from the back. Each side looks like an encrypted volume
  that is mostly free space.
* Each side has a small encrypted **plain volume**. It's keyed by Argon2id(password, salt) and found by
  trying that key at a fixed offset. It holds the side's data-region key, boot scripts, and tools. The
  boot USB only knows the decoy's offset.
* **Booting the decoy:** boot the USB and type the decoy password.
* **Booting the hidden system:** press Enter at the password prompt to fall into the "recovery" shell,
  change one number with a one-line `sed`, re-run the boot script, and type the hidden password.
  Nothing on the USB refers to the hidden system. The only hidden-specific information is the one number
  you keep in your head.
* The boot USB runs a small Linux environment that unlocks the chosen side and `kexec`s into that
  side's own kernel. The decoy and hidden systems are separated by a full OS boundary, so shell
  history, caches, and logs on one side never contain anything from the other.

The details are in [docs/design.md](docs/design.md).

## Status

| | |
|---|---|
| **v1** (the current code: `pd.py`, `early.sh`, `late.sh`, …) | Works, including dual-booting Arch and NixOS on one side. Needs root, has hard-coded config, and builds with the host's dracut and kernel. Documented as-built in [docs/v1.md](docs/v1.md). |
| **v2** (in progress on branch `v2`) | Nix-pinned static build, minimal boot USB, LUKS dropped, rootless QEMU test suite, setup in throwaway environments. Specified in [docs/design.md](docs/design.md); progress tracked in [docs/roadmap.md](docs/roadmap.md). |

> [!WARNING]
> This is a research project. **The repository is public**, so its boot scripts are recognizable. An
> adversary who knows about pd can identify a pd boot USB and will assume a hidden system exists. See
> [docs/threat-model.md](docs/threat-model.md) for what pd does and doesn't protect against.

## Documentation

| Doc | Contents |
|---|---|
| [docs/design.md](docs/design.md) | v2 design, the reasoning behind every decision, decision log |
| [docs/threat-model.md](docs/threat-model.md) | Adversaries, guarantees, what is out of scope, operational rules |
| [docs/booting.md](docs/booting.md) | Decoy boot, hidden boot, boot menus, recovery |
| [docs/v1.md](docs/v1.md) | Exact as-built description of v1 (what deployed installs run) |
| [docs/migration.md](docs/migration.md) | Moving a v1 install to v2 |
| [docs/roadmap.md](docs/roadmap.md) | Milestones and ideas |
| [docs/history.md](docs/history.md) | How the design evolved, reconstructed from the git history |
| [AGENTS.md](AGENTS.md) | Orientation and rules for coding agents working on this repo |

## Quick start

v2 isn't usable yet. The planned workflow ([docs/design.md §9](docs/design.md#9-setup)):

1. On a development machine, build the artifacts: `nix build` (pinned kernel, static tools, a
   UKI template).
2. Boot the target machine into a live ISO. Run `pd init --decoy`, install the decoy OS into `/mnt`,
   then `pd close`.
3. Run `pd init --hidden`, install the hidden OS into `/mnt`, then `pd close`. Memorize the printed
   hidden offset.
4. Run `pd usb /dev/sdX` to write the boot UKI to a USB stick.

v1 is configured by editing the constants at the top of `pd.py` and must be run as root. See
[docs/v1.md](docs/v1.md).

