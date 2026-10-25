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
  system's boot logic lives only in the hidden plain volume. The only hidden-specific value, the offset,
  lives in the user's head.

> [!WARNING]
> **This repository is public.** An adversary who recognizes the USB's scripts as pd knows a hidden
> system is *possible*. Because pd makes absence impossible to prove, a coercer who knows about pd may
> simply keep coercing. pd remains a research project until there is a variant whose boot USB can't be
> told apart from a stock setup ([design.md §12](design.md#12-alternatives-considered), last row).

## Out of scope

* **Snapshot adversaries.** Two or more disk images taken at different times show "free" space
  changing. Never clone the disk. Back up hidden data only to another pd setup.
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
| pd's scripts on the USB | Recognizable now that the repo is public (see A2). |
| No TRIM on an SSD | Common advice for encrypted SSDs; dm-crypt disables discards by default because they leak which blocks are in use. |
| A shutdown `fstrim` unit in the decoy | An ordinary way to reclaim thin pool space; the pool's `nopassdown` mode is a documented LVM setting. |
| A barely used decoy | Use the decoy regularly, with realistic data. A decoy with no activity for months, on a machine that is clearly used, is suspicious. |
| SMART counters (power-on hours, power cycles, data written) exceed what the decoy's logs explain | "I reinstalled the OS" explains totals from before the decoy's install date. Keep that date plausible. |
| Network-side evidence (DHCP leases, Wi-Fi logs, account activity) from times the decoy says the machine was off | Outside pd's control; be aware of it. |
| Leftover traces on the machine that ran setup | Run setup only from the RAM-only setup UKI ([install.md](install.md)). Wipe the setup stick afterwards: it holds `pd.toml`. |
| LVM metadata history in the decoy | v2 creates the decoy with stock commands and names ([design.md §6.1](design.md#61-decoy-stock-lvm)). A v1 decoy's history shows pd's unusual commands. |
| Filesystem errors in the decoy | If the hidden side ever overwrote decoy blocks, the decoy would show checksum errors. Prevented by the hidden cap and the frontier monitor. |
| A written-down hidden offset | Never write it down near the machine, on the USB, or in the decoy. |

## Operational rules

1. Use the decoy regularly.
2. Never image or clone the disk, and never use LVM thin snapshots. (btrfs snapshots are fine.)
3. Watch the space. The pool's usage (`lvs`) is what counts, not `df`; the frontier monitor reports the
   gap on every hidden boot. Keep it wide.
4. Run setup only in throwaway environments.
5. Keep the hidden offset in your head.
6. Use different decoy and hidden passwords (setup enforces this).
7. Don't enable discards beyond the shutdown trim: no `discard` mount option, no `fstrim` timer, and
   never `allow_discards` on dm-crypt.
8. Remember the boot environment uses the US keyboard layout when you type passwords.

