# Installing pd (v2)

> [!WARNING]
> v2 is tested end to end in QEMU only (see [design.md §11](design.md#11-build-and-test)). The steps
> marked *(tested)* are covered by the test suite; the rest follow from the same code but haven't been
> run on real hardware. Rehearse on a spare disk first.

## Overview

| Step | Where | Result |
|---|---|---|
| 1. Build | any machine with Nix | the setup UKI and the boot environment |
| 2. Configure | anywhere | `pd.toml` |
| 3. Build the boot USB | the build machine | `BOOTX64.EFI` on the boot stick |
| 4. Set up the disk | the target machine, booted from the setup stick | randomized disk, decoy and hidden sides |
| 5. Install the OSes | the same session | Arch and/or NixOS on each side |
| 6. Boot | the target machine, booted from the boot stick | [booting.md](booting.md) |

You need two USB sticks: a **setup stick**, used once and then wiped, and the **boot stick** you keep.
Each needs a FAT32 EFI system partition. Installing the OSes needs a wired network: the setup
environment has drivers for common Intel, Realtek, Broadcom and Aquantia NICs and USB Ethernet
adapters, but no Wi-Fi.

## 1. Build

On any x86_64 Linux machine with Nix:
```sh
nix build "path:$PWD#setupUki" --out-link setup.efi   # RAM-only setup environment (about 180 MB)
nix build "path:$PWD#testenv"  --out-link env         # boot environment pieces, used by `pd usb`
```
(`path:` is needed because Nix's libgit2 can't read this repository's ref format; pass
`--extra-experimental-features 'nix-command flakes'` if flakes aren't enabled.)

## 2. Configure

`pd.toml` holds everything specific to the install. Keep it off the target disk and out of the repo.
It contains no secrets except the salt, which also ends up on the boot stick.

```toml
disk = "/dev/nvme0n1"          # the target disk, as seen from the setup environment
salt = "…"                     # `python3 -m pd salt` (or `pd salt` in the setup environment)
vg = "sysvg"                   # VG name, the same on both sides
hostname = "laptop"            # optional: set during setup, since LVM records it (use the decoy's)
swap_size = "16G"              # swap LV on each side, fixed at setup (default: this machine's RAM; "0": none)
hidden_cap = 0.4               # hidden thin LVs may use at most this fraction of the disk, in total
min_gap_percent = 10           # frontier monitor warns below this much free space between the sides
memorable_offset = true        # round the hidden offset to a multiple of 10^6 sectors (default)
keymap = "de"                  # optional console keymap for the password prompt (kbd names; default US)

[argon2]                       # optional; the defaults take about 0.3 s (design.md §4)
t = 1
m_log2 = 20                    # memory = 2^m_log2 KiB (20 = 1 GiB)
p = 4

[[systems]]                    # one entry per OS on each side (both sides use the same list)
name = "nixos"
lv = "nixroot"                 # its btrfs root LV
kernel = "/boot/default/kernel"   # paths inside the root's @boot subvolume, as /boot/...
initrd = "/boot/default/initrd"
cmdline = ""                   # NixOS: the generation menu supplies init= and parameters

[[systems]]
name = "arch"
lv = "root"
kernel = "/boot/vmlinuz-linux"
initrd = "/boot/initramfs-linux.img"
cmdline = "root=/dev/sysvg/root rootflags=subvol=@ rw"

[[volumes]]                    # optional extra thin LVs
name = "data"
fs = "xfs"                     # (default) or "btrfs"; see design.md §2.3
```

`default` (the system booted without holding Shift) is the first `[[systems]]` entry unless set.
`subvolumes` overrides the btrfs subvolume layout of the roots; the default is `@`, `@boot`, `@home`,
`@home_snapshots`, `@nix`, `@snapshots`, `@var_cache`, `@var_lib_docker`, `@var_log`, `@var_tmp`, and
the boot path needs `@boot` (and `@nix` for NixOS).

On btrfs, files that get rewritten in place (VM images, databases) make the filesystem grow to
more than twice their size, and the side's footprint on the disk grows with it. Give them a directory
with `chattr +C` (before creating the files), or put them on an XFS volume (design.md §2.3).

## 3. Build the boot USB

```sh
python3 -m pd usb --config pd.toml --env env --output BOOTX64.EFI
```
Copy `BOOTX64.EFI` to `EFI/BOOT/BOOTX64.EFI` on the boot stick's EFI system partition. The UKI holds the
kernel, the boot initrd, the salt, and `early_env.sh`; nothing about the hidden side, and nothing about
the machine that built it. *(tested: `test_uefi` boots it from a USB stick under OVMF.)*

To rebuild it later (e.g. to update the boot environment), take the salt from the old one instead of
the config: `pd usb --config pd.toml --env env --output NEW.EFI --salt-from OLD.EFI`.

## 4. Set up the disk

Copy `setup.efi` to `EFI/BOOT/BOOTX64.EFI` on the setup stick, along with `pd.toml`. Boot the target
machine from it. You get a root shell in RAM:
```sh
mkdir /m && mount /dev/sda1 /m        # the setup stick; check with lsblk
pd randomize --config /m/pd.toml      # fills the whole disk; hours on a large disk
pd init-decoy --config /m/pd.toml --run
```
`init-decoy` asks for the decoy password, creates the decoy side, and opens a shell with each system's
root mounted at `$PD_ROOT/<system>` (all subvolumes; `$PD_SRC` points at pd's `target/` files) and each extra volume at `$PD_ROOT/volumes/<name>`.
Install the decoy OSes now (§5), then `exit`.
```sh
pd init-hidden --config /m/pd.toml --run
```
`init-hidden` asks for the decoy password (to read the decoy's layout and store its data key for the
frontier monitor) and the hidden password, creates the hidden side, and prints the hidden-boot command,
e.g. `sed -i s/2048/1945000000/ /early_env.sh`. **Remember the number; don't write it down.** Install
the hidden OSes, `exit`, and power off (`poweroff -f`).
*(tested: `test_setup_env` runs randomize and init-decoy from the setup environment.)*

Later maintenance, from the setup environment or a live ISO (below):
* `pd open --config pd.toml [--offset N] [--rw]` opens a side (the hidden side with `--offset` set to
  its start sector) and mounts each system's root, as its full subvolume tree, at
  `/mnt/pd/<system>`, and extra volumes at `/mnt/pd/volumes/<name>`. It's **read-only by default** and
  then writes nothing to the disk (the same approach as the boot environment); `--rw` activates the
  VG and mounts read-write. `pd close` undoes everything. `--run CMD` opens, runs CMD, and closes.
* `pd rekey --config pd.toml [--offset N] [--new-salt S]` changes a side's password and/or the salt.
  With a new salt, rekey both sides, put the salt in `pd.toml`, and build a new boot USB.
* `pd init-decoy --force --hidden-offset N` re-creates the decoy (its data is lost) while keeping the
  hidden side; `--force --no-hidden` if there is none.
* `pd compact --config pd.toml [--offset N]` moves a side's data to the front of its thin pool, in
  place. `pd init-hidden` asks for it when the decoy has data where the hidden side would go, which
  happens once a decoy has been nearly full. It trims the filesystems first, then moves only what's
  above the boundary; interrupted, it leaves the pool intact (`--recover` handles the one unsafe moment).
* `pd move --config pd.toml --to /dev/NEWDISK (--hidden-offset N | --no-hidden)` moves both sides to
  another disk, bigger or smaller, and prints the hidden side's new offset. The boot USB keeps working.
  Wipe the old disk afterwards (`pd randomize`).

## 5. Install the OSes

Run `pd-net` first to bring up wired networking with DHCP. It also sets up Arch's signing keys
(shipped in the setup environment, pinned by hash), so `pacstrap` verifies every package; the mirror
is Arch's own `geo.mirror.pkgbuild.com`, which `pacstrap` copies into the installed system.
*(tested: `test_setup_env` with `PD_TEST_NET=1` gets a DHCP lease through an emulated Intel NIC and
runs `pacstrap -K` with signature checks.)* Every OS on a pd disk needs:
* **the pd unlock hook** in its initrd, which opens the disk with the keys pd hands over;
* **`nodiscard`** on every filesystem, and **no periodic trim** (`fstrim.timer`, `services.fstrim`);
* **one `fstrim` at shutdown**, which keeps each side's footprint near its live data
  ([design.md §2.3](design.md#23-discards-never-to-the-disk-and-to-the-pool-only-at-shutdown)).

### Arch Linux *(tested: `test_arch` installs Arch with `pacstrap` and the `pd` hook, then boots it through pd)*
```sh
r=$PD_ROOT/arch
pacstrap -K "$r" base linux linux-firmware mkinitcpio lvm2 cryptsetup btrfs-progs
install -Dm755 $PD_SRC/target/hook.sh "$r/usr/lib/pd/hook.sh"
install -Dm644 $PD_SRC/target/arch/pd-install "$r/etc/initcpio/install/pd"
install -Dm644 $PD_SRC/target/arch/pd-hook "$r/etc/initcpio/hooks/pd"
install -Dm644 $PD_SRC/target/arch/fstrim-before-umount.service "$r/etc/systemd/system/"
sed -i 's/^HOOKS=.*/HOOKS=(base udev autodetect microcode modconf kms keyboard keymap consolefont block pd lvm2 filesystems fsck)/' \
    "$r/etc/mkinitcpio.conf"
genfstab -U "$r" | sed -E 's/\t(btrfs|xfs)\t([^\t]*)/\t\1\t\2,nodiscard/' >> "$r/etc/fstab"
arch-chroot "$r" sh -c 'systemctl enable fstrim-before-umount.service; mkinitcpio -P; passwd'
```
Check `$r/etc/fstab` by hand: every btrfs and XFS entry needs `nodiscard`. The `pd` hook targets the
default busybox-based initramfs (`udev` hook), not the `systemd` hook. With `autodetect`, mkinitcpio
only includes drivers for the machine it runs on, which is the target machine here.

### NixOS *(the module is tested with a real NixOS initrd; a full install isn't)*
Import the module and enable it, either from the flake (`pd.nixosModules.default`) or by path
(`target/nixos/pd.nix`):
```nix
{
  imports = [ ./pd.nix ];
  pd.enable = true;              # unlock hook; also sets boot.loader.generationsDir, thin LVM
  # pd.trimOnShutdown = true;    # default: fstrim once at shutdown; services.fstrim off
  # pd.v1Compat = false;         # also accept v1 boot USBs, during migration only

  fileSystems."/" = {
    device = "/dev/sysvg/nixroot";
    fsType = "btrfs";
    options = [ "subvol=@" "noatime" "nodiscard" "compress=zstd" ];
  };
  fileSystems."/boot" = {
    device = "/dev/sysvg/nixroot";
    fsType = "btrfs";
    options = [ "subvol=@boot" "noatime" "nodiscard" ];
    neededForBoot = true;
  };
  fileSystems."/nix" = {
    device = "/dev/sysvg/nixroot";
    fsType = "btrfs";
    options = [ "subvol=@nix" "noatime" "nodiscard" "compress=zstd" ];
  };
  # … one entry per subvolume (@home, @var_log, …) and per extra volume
  swapDevices = [ { device = "/dev/sysvg/swap"; } ];
}
```
Then:
```sh
nixos-generate-config --root $PD_ROOT/nixos      # hardware-configuration.nix; replace its fileSystems
# add the configuration above
nixos-install --root $PD_ROOT/nixos              # or --flake <path>#<host>
```
The module warns at build time about filesystems without `nodiscard` and about `services.fstrim`. Don't
put `root=` in the kernel parameters: NixOS mounts root from `fileSystems."/"`.

## Without pd's USB

[stock-boot.md](stock-boot.md) describes a variant with no pd USB: the decoy is a stock LUKS + LVM thin
install that boots on its own, `pd init-hidden --stock-decoy` adds the hidden side, and the hidden
side is booted from a stock live system with commands typed from memory.

## FIDO2 security keys (optional)

A FIDO2 security key with `hmac-secret` (e.g. any YubiKey 5 or Security Key) makes offline password
guessing impossible without the key ([design.md §4.1](design.md#41-fido2-security-keys-optional)).
Like `systemd-cryptenroll`, pd needs the key's PIN and a touch. Set a PIN first (e.g. `ykman fido
access change-pin`). Then, in the setup environment, after setting up the sides:

1. Add `fido2 = "fido2.txt"` to the config, at the top level (before any `[table]`).
2. Enroll the decoy: `pd fido2 enroll --config pd.toml`. It asks for the decoy password and the PIN,
   and for a touch. Then it offers to enroll another key: swap keys and press Enter. Enroll at least
   two keys: losing every enrolled key loses the side.
3. If there's a hidden side, enroll it too, with the same keys: `pd fido2 enroll --config pd.toml
   --offset N`. If the hidden side uses FIDO2, the decoy must too (otherwise `fido2.txt` on the USB
   points at another side).
4. Build the boot USB again (§3). `fido2.txt` goes on it, and the boot asks for the PIN after the
   password.

The first enrollment re-creates the side's boot volume under a random key. After that the password alone
no longer opens it. To add a key later, plug in an enrolled one first. `pd rekey` re-wraps every key
it's shown, one at a time; keys it isn't shown stop working for that side. `pd fido2 list` and `pd
fido2 remove N` manage the list. Rebuild the USB after every change. *(tested: `test_fido2`, with
QEMU's emulated CanoKey; not yet with a real security key.)*

## From a live ISO

`pd open`, `pd close` and `pd rekey` also run from an ordinary live system such as Debian Live or
Tails, with pd as a single file:
```sh
nix build "path:$PWD#pdPyz" --out-link pd.pyz     # on the build machine; copy pd.pyz and pd.toml over
sudo apt install cryptsetup lvm2 thin-provisioning-tools btrfs-progs argon2   # on the live system
sudo python3 pd.pyz open --config pd.toml          # read-only; --rw to change things
sudo python3 pd.pyz close
```
With FIDO2 security keys, also copy the static `fido2-hmac` (`nix build "path:$PWD#fido2Hmac"`) onto the
`PATH`. Setting up a disk (`randomize`, `init-decoy`, `init-hidden`) needs the setup UKI, which also
carries pd's boot-volume tools. A live system keeps no trace on the disk, but think about what it logs
elsewhere (e.g. Tails' persistent storage, if enabled). *(tested: `test_open` runs `pd.pyz` in the
tool VM; not yet on an actual Debian Live or Tails image.)*

## 6. Boot

Remove the setup stick (and wipe it), insert the boot stick, and follow [booting.md](booting.md). On
every hidden boot the frontier monitor prints the free space between the two sides.
