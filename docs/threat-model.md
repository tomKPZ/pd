# Threat model

## What pd protects

* **Primary asset:** the existence of the hidden system, and with it the hidden system's contents.
* **Secondary asset:** the decoy's data, against anyone who doesn't have the decoy password. Under
  coercion the decoy is meant to be given up.

## Adversaries

### A1. Finder (in scope)
Has the machine or the bare disk, maybe also the USB, but no passwords. Wants to show that anything is
installed at all.

What they see:
* a disk full of random data, with a zeroed first MiB and no partition table. It looks like a drive
  that was wiped or never provisioned;
* no NVRAM boot entry pointing at a USB, because the boot environment deletes the firmware's entry on
  every boot.

If they also have the USB, the caveat under A2 applies.

### A2. Coercer (in scope; the main design target)
Gets the boot USB and the decoy password, through seizure plus compelled disclosure, a border search, or
physical coercion. Can image the disk once and study it at leisure, and knows how Linux, LVM, and
dm-crypt work.

What they see is a complete, ordinary-looking system:
* The USB boots a small Linux environment. It asks for a password, opens an encrypted volume holding
  boot scripts, and those open an LVM-on-dm-crypt system.
* A thin pool spans the disk and is mostly free.
* The random data in free space is explained by the pre-randomization careful users do before
  encrypting a disk.
* No file, script, log, or metadata in the decoy or on the USB refers to a hidden system. The hidden
  system's boot logic lives only in the hidden plain volume.

**The hidden offset is not a secret.** Someone holding the decoy can compute where a hidden boot volume
would sit: just below the decoy pool's `tmeta` (v1: at D − 256 MiB). The offset is kept off the USB so
that nothing there *points* at a hidden side, not because it's hard to guess. What protects the hidden
side is that, without the hidden password, its boot volume and data are indistinguishable from the
random fill around them. An adversary who suspects pd can find the spot, but can't show anything is
there.

> [!WARNING]
> **This repository is public.** An adversary who recognizes the USB's scripts as pd knows a hidden
> system is *possible*. Because pd makes absence impossible to prove, a coercer who knows about pd may
> simply keep coercing. [Stock boot](stock-boot.md) avoids pd's USB altogether: the decoy is a stock
> install, and the hidden side is booted from a stock live system with commands typed from memory. Its
> configuration can still hint at it ([stock-boot.md](stock-boot.md#what-could-give-you-away)).

### Offline password guessing
Anyone with the USB and a disk image (A1 or A2) can guess passwords offline: the salt is on the USB, and
a guess is checked by trying to open a boot volume. Argon2 (1 GiB, one pass by default: about 0.3 s, and
1 GiB of memory per guess, which limits how many guesses a GPU can run at once) makes each guess
costly. The defaults favor a short boot over a slow KDF, so the password has to be correspondingly
strong: each extra random character multiplies an attacker's work by the size of the character set
(about 60–90), far more than any practical Argon2 setting. With FIDO2 security keys enrolled ([design.md §4.1](design.md#41-fido2-security-keys-optional)),
each guess also needs the physical key, its PIN (the key locks after 8 wrong tries), and a touch. That
doesn't change coercion: a coercer demands the key along with the password.

## Out of scope

* **Snapshot adversaries.** Two or more disk images taken at different times show "free" space
  changing. Never clone the disk. Back up hidden data only to another pd setup. The boot USB counts
  too: `fido2.txt` changes when the hidden side enrolls a security key or is re-keyed, while the
  decoy's slots stay the same. Someone who kept a copy of an earlier USB can compare. Re-key or
  enroll the decoy at the same time, and don't let old copies of the USB exist.
* **Flash forensics.** SSD wear leveling, FTL mapping tables, chip-off analysis. If this matters, two
  mitigations:
  * a self-encrypting drive with a hardware security boundary, such as a FIPS 140-2 level 3 drive like
    the Apricorn Aegis;
  * a mechanical disk, which also makes "why no TRIM?" moot.
* **RAM attacks:** cold boot, DMA, or access to a running unlocked system. `init_on_free=1` and
  shredding reduce key residue, but they aren't a defense.
* **Evil maid, firmware implants, keyloggers,** and anyone watching you type the hidden-boot command.

## Things that could give you away

| Signal | Explanation or mitigation |
|---|---|
| pd's scripts on the USB | Recognizable now that the repo is public (see A2). [Stock boot](stock-boot.md) has no pd USB. |
| No TRIM on an SSD | Common advice for encrypted SSDs; dm-crypt disables discards by default because they leak which blocks are in use. |
| A shutdown `fstrim` unit in the decoy | An ordinary way to reclaim thin pool space; the pool's `nopassdown` mode is a documented LVM setting. |
| A barely used decoy | Use the decoy regularly, with realistic data. A decoy with no activity for months, on a machine that is clearly used, is suspicious. |
| SMART counters (power-on hours, power cycles, data written) exceed what the decoy's logs explain | "I reinstalled the OS" explains totals from before the decoy's install date. Keep that date plausible. |
| Network-side evidence (DHCP leases, Wi-Fi logs, account activity) from times the decoy says the machine was off | Outside pd's control; be aware of it. |
| Leftover traces on the machine that ran setup | Run setup only from the RAM-only setup UKI ([install.md](install.md)). Wipe the setup stick afterwards: it holds `pd.toml`. |
| LVM metadata history in the decoy | v2 creates the decoy with stock commands and names ([design.md §6.1](design.md#61-decoy-stock-lvm)). A v1 decoy's history shows pd's unusual commands. Each version also records the `uname` of the machine that ran the command: for a pd decoy, the setup environment's NixOS kernel. That marks the decoy as set up by pd, which the USB shows anyway, not the presence of a hidden side. |
| The decoy's old data in the hidden side's space | Blocks the decoy freed keep their old contents, and its pool metadata keeps old pointers to them; hidden data written over them would show. `pd compact` and `init-hidden` on a used decoy clear these traces; keep the decoy's data below the hidden side's floor, which the frontier monitor checks ([design.md §10.5](design.md#105-traces-of-the-decoys-old-data)). |
| Filesystem errors in the decoy | If the hidden side ever overwrote decoy blocks, the decoy would show checksum errors. Prevented by the hidden cap and the frontier monitor. |
| `fido2.txt` on the USB, but the decoy doesn't use a security key | Points at a side that does. If the hidden side uses FIDO2, enroll the decoy too. |
| A written-down hidden offset | Not a secret in itself, but a note of it next to the machine is a pointer saying "there is a hidden side". Keep it out of the decoy, off the USB, and away from the machine. |

## Operational rules

1. Use the decoy regularly.
2. Never image or clone the disk, and never use LVM thin snapshots. (btrfs snapshots are fine.)
3. Watch the space. The pool's usage (`lvs`) is what counts, not `df`; the frontier monitor reports the
   gap on every hidden boot. Keep it wide.
4. Run setup only in throwaway environments.
5. Remember the hidden offset rather than writing it near the machine (by default it's a round number).
6. Use different decoy and hidden passwords (setup enforces this).
7. Don't enable discards beyond the shutdown trim: no `discard` mount option, no `fstrim` timer, and
   never `allow_discards` on dm-crypt.
8. Remember the boot environment's keyboard layout when you type passwords: US unless `keymap` is set
   in the config. (The keymap on the USB reveals only your layout.)

