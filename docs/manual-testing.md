# Manual testing on real hardware

This is a step-by-step plan for validating Plain Disguise (pd) on a real machine: pd's own boot USB
(part B), FIDO2 with a YubiKey (part C), and stock boot (part D). Everything else is covered by the
QEMU test suite ([design.md §11](design.md#11-build-and-test)); these parts need real firmware,
keyboards, storage, a real security key and real live images.

> [!CAUTION]
> Every part destroys the data on the target disk. Use a spare machine or a spare disk, and
> physically disconnect every other disk (especially any disk with a deployed v1 install) before you
> start. Use throwaway passwords and PINs; this plan has you write them down.

| Part | What it validates | Time |
|---|---|---|
| A. Preparation | builds, USB sticks, firmware settings | 30–60 min |
| B. pd's USB | setup environment, both sides, boot, menus, trim, crash recovery | 2–3 h (randomizing takes 10–60 min) |
| C. YubiKey | FIDO2 enrollment, boot with PIN and touch, failure cases, rekey | 45 min |
| D. Stock boot | a stock decoy, the hidden side added next to it, boots from Debian Live and Tails | 2–3 h |
| F. Optional: compaction and moving | adding a hidden side to a decoy that was once full; `pd move` | 1–2 h |
| E. Report | what to send back | 10 min |

Each step lists what to do and, as checkboxes, what to expect. Note anything that differs, with the
exact message (a phone photo of the screen is fine), in the results table at the end.

---

## A. Preparation

### A1. Build (on the build machine, in the repository, branch `v2`)

```sh
NIX="nix --extra-experimental-features nix-command --extra-experimental-features flakes"
$NIX build -j auto "path:$PWD#setupUki" --out-link setup.efi
$NIX build -j auto "path:$PWD#bootUki"  --out-link boot.efi
$NIX build -j auto --rebuild "path:$PWD#bootUki"   # succeeds only if the rebuild is bit-for-bit identical
sha256sum -L boot.efi                    # record it
```

### A2. Write `pd.toml` (on the build machine, outside the repository)

```toml
disk = "/dev/nvme0n1"          # the target disk as the setup environment will see it (fixed in B2 if needed)
vg = "sysvg"
memorable_offset = true

[[systems]]
name = "arch"
lv = "root"
kernel = "/boot/vmlinuz-linux"
initrd = "/boot/initramfs-linux.img"
cmdline = "root=/dev/sysvg/root rootflags=subvol=@ rw"
```

* Swap defaults to the target machine's RAM size, and the Argon2 defaults are pd's (about 0.3 s).
* If you type passwords on a non-US keyboard layout, add `keymap = "de"` (or yours) now: it goes into
  the boot USB.
* To also test NixOS (optional, B7), add a second `[[systems]]` entry now, since the list is fixed
  when the sides are created:
  ```toml
  [[systems]]
  name = "nixos"
  lv = "nixroot"
  kernel = "/boot/default/kernel"
  initrd = "/boot/default/initrd"
  cmdline = ""
  ```

### A3. The boot image (on the build machine)

The boot image is the same for every install, so nothing is built for this one:
```sh
cp -L boot.efi BOOTX64.EFI
```

Copy `setup.efi` (follow the symlink: `cp -L`), `BOOTX64.EFI` and `pd.toml` to the machine where you
write the USB sticks. (The setup environment carries the same image: `pd usb` there writes it too, as
in C3.)

### A4. Write the USB sticks (on a Linux machine)

Find each stick's device name with `lsblk -o NAME,SIZE,MODEL,TRAN` (TRAN is `usb`), and double-check
it: these commands erase the stick. `/dev/sdX` stands for the stick.

**Setup stick** (pd's setup environment, plus `pd.toml`):
```sh
sudo wipefs -a /dev/sdX
printf 'label: gpt\ntype=U\n' | sudo sfdisk /dev/sdX
sudo mkfs.vfat -F 32 /dev/sdX1
sudo mount /dev/sdX1 /mnt
sudo mkdir -p /mnt/EFI/BOOT
sudo cp setup.efi /mnt/EFI/BOOT/BOOTX64.EFI
sudo cp pd.toml /mnt/
sudo umount /mnt
```

**Boot stick** (pd's boot USB): the same commands, but copy `BOOTX64.EFI` to `EFI/BOOT/BOOTX64.EFI`
and nothing else. Don't give it a label.

**For part D only:**
* **Debian Live**: the Xfce image (`debian-live-13.*-amd64-xfce.iso` from
  `https://cdimage.debian.org/debian-cd/current-live/amd64/iso-hybrid/`, checked against `SHA256SUMS`).
  The "standard" image has no cryptsetup. Write it with
  `sudo dd if=debian-live-…-xfce.iso of=/dev/sdX bs=4M conv=fsync status=progress`.
* **Tails 7**: the USB image from tails.net, verified as its site describes, written the same way.
* **A decoy installer** if you follow D2's Fedora option: the Fedora Everything or Server installer
  ISO, written with `dd`. The manual option D2b needs no extra stick.

You can reuse a stick once you're done with it (e.g. the Fedora stick for Tails).

### A5. The target machine

* [ ] Disconnect every disk except the target disk.
* [ ] Firmware settings: **Secure Boot off** (pd's UKIs aren't signed, and kexec needs it off), **Fast
  Boot off** (so USB sticks are seen), booting from USB allowed.
* [ ] If the firmware has a storage mode setting (Intel RST/VMD vs AHCI), note it. pd has the VMD driver,
  so leave it; if the disk isn't found in B1, switch to AHCI and retry.
* [ ] A wired network: the built-in Ethernet port or a USB Ethernet adapter, with DHCP. The setup
  environment has no Wi-Fi.
* [ ] Note the machine model, firmware version, disk model and size, RAM, and the NIC (results table).

---

## B. pd's boot USB

### B1. Boot the setup environment

Plug in the setup stick and choose it in the firmware's boot menu.

* [ ] The screen shows `pd setup environment (RAM only; nothing here persists)` and a `bash` prompt.
* [ ] The keyboard works.
* [ ] `lsblk -o NAME,SIZE,MODEL` lists the target disk (and the setup stick).
* [ ] `ip link` lists the wired interface (e.g. `enp…`, `eth0`).

### B2. Mount the setup stick and check the config

```sh
mkdir /m && mount /dev/sda1 /m        # the setup stick: the small vfat disk in lsblk
grep disk /m/pd.toml                  # must name the target disk as lsblk shows it; fix with vi if not
```

### B3. Network

```sh
pd-net
```

* [ ] It returns without "no DHCP lease" or keyring warnings. (`ping -c1 archlinux.org` should work.)
  This validates the NIC drivers.

### B4. Randomize the disk

```sh
pd randomize --config /m/pd.toml
```

* [ ] It counts up to 100% and returns without an error. Record the time it took.

```sh
od -An -tx1 -N 64 /dev/nvme0n1                   # random bytes, not zeros
wipefs -n /dev/nvme0n1                           # prints nothing
```
* [ ] As in the comments.

### B5. Create the decoy side and install Arch on it

```sh
pd init-decoy --config /m/pd.toml --run
```

* [ ] It asks for the decoy password twice and opens a shell.

In that shell:
```sh
r=$PD_ROOT/arch
pacstrap -K "$r" base linux linux-firmware mkinitcpio lvm2 cryptsetup btrfs-progs efibootmgr vim
install -Dm755 $PD_SRC/target/hook.sh "$r/usr/lib/pd/hook.sh"
install -Dm644 $PD_SRC/target/arch/pd-install "$r/etc/initcpio/install/pd"
install -Dm644 $PD_SRC/target/arch/pd-hook "$r/etc/initcpio/hooks/pd"
install -Dm644 $PD_SRC/target/arch/fstrim-before-umount.service "$r/etc/systemd/system/"
sed -i 's/^HOOKS=.*/HOOKS=(base udev autodetect microcode modconf kms keyboard keymap consolefont block pd lvm2 filesystems fsck)/' \
    "$r/etc/mkinitcpio.conf"
genfstab -U "$r" | sed -E 's/\t(btrfs|xfs)\t([^\t]*)/\t\1\t\2,nodiscard/' >> "$r/etc/fstab"
echo '/dev/sysvg/swap none swap defaults 0 0' >> "$r/etc/fstab"
echo testdecoy > "$r/etc/hostname"
arch-chroot "$r" sh -c 'systemctl enable fstrim-before-umount.service; mkinitcpio -P; passwd'
exit
```

* [ ] `pacstrap` shows "checking keys in keyring" and "checking package integrity" without errors.
* [ ] `mkinitcpio` builds both images without errors about the `pd` hook.
* [ ] `grep nodiscard $r/etc/fstab` shows it on every btrfs line (check before `exit`).

### B6. Create the hidden side and install Arch on it

```sh
pd init-hidden --config /m/pd.toml --run
```

* [ ] It asks for the decoy password once and the hidden password twice, and opens a shell.

Repeat the B5 commands in that shell, with `echo testhidden` instead of `testdecoy`, then `exit`.

* [ ] It prints `Hidden boot: press Enter at the password prompt, then run:` and
  `sed -i s/2048/NNNNNNNNNN/ /early_env.sh`. **Write the number down** (it's a test disk); it's a
  multiple of 1,000,000.

### B7. Optional: NixOS on the hidden side

Only if you added the `nixos` entry in A2: in B6's shell, before `exit`, also follow
[install.md](install.md) "NixOS" for `$PD_ROOT/nixos` (`nixos-generate-config`, the pd module,
`nixos-install`). Use `/dev/sysvg/nixroot` in its `fileSystems`.

* [ ] `nixos-install` completes, and the module prints no warnings about `nodiscard` or `fstrim`.

### B8. Power off

```sh
sync; umount /m; poweroff -f
```

Remove the setup stick.

### B9. Boot the decoy

Plug in the boot stick and choose it in the boot menu.

* [ ] `Password:` appears within a few seconds, and typing doesn't echo.
* [ ] After the decoy password, the boot continues almost at once (Argon2 is about 0.3 s), and Arch
  boots to the `testdecoy login:` prompt.

Log in as root and check:
```sh
cat /etc/hostname                                  # testdecoy
lsblk --discard                                    # DISC-MAX 0 for cryptlvm and everything below it
systemctl is-enabled fstrim-before-umount fstrim.timer   # enabled; disabled or not-found
swapon --show                                      # /dev/dm-… (sysvg-swap), RAM-sized
lvs -o lv_name,lv_size,data_percent sysvg          # note thinpool's data%
efibootmgr                                         # note whether a USB entry is listed
```
* [ ] All as described in the comments. For `efibootmgr`, record what you see: pd deletes the boot
  entry the firmware used for the stick, but some firmware recreates one.

### B10. Shutdown trim

In the decoy:
```sh
dd if=/dev/urandom of=/root/big bs=1M count=4096 && sync
lvs -o lv_name,data_percent sysvg                  # thinpool's data% went up
rm /root/big && sync
lvs -o lv_name,data_percent sysvg                  # unchanged: no discards during a session
poweroff
```
Boot the decoy again (B9) and run `lvs -o lv_name,data_percent sysvg`.

* [ ] thinpool's data% is back near where it was before the 4 GiB file.

### B11. Boot the hidden side

Boot the boot stick. At `Password:` press Enter without typing anything.

* [ ] You land in the recovery shell immediately.

```sh
sed -i s/2048/NNNNNNNNNN/ /early_env.sh
/early.sh
```

* [ ] After the hidden password it prints `Free space between the two sides: N MiB.` without a
  warning, and Arch boots to `testhidden login:`.
* [ ] Repeat the B9 checks (`lsblk --discard`, the trim units, `swapon --show`).

### B12. Menus, wrong password, timing

* [ ] **System menu:** boot the decoy and hold Shift right after pressing Enter on the password. A
  menu titled "Boot System" appears (with only `arch`, it lists `default` and `arch`). Choose one;
  it boots. (With NixOS from B7, choosing `nixos` while still holding Shift shows its generation menu.)
* [ ] **Wrong password:** type a wrong password. It prints `No bootable disk found.` and starts the
  recovery shell; `exit` powers the machine off.
* [ ] **Argon2 timing:** at `Password:` press Enter for the recovery shell, then run:
  ```sh
  . /early_env.sh; . /disk_class.sh; S=$(disk_salt /dev/nvme0n1); echo "${#S} $(disk_class /dev/nvme0n1)"
  time sh -c "printf test | argon2 $S $ARGON2_ARGS >/dev/null"
  ```
  Record the time (expected: well under a second). The first line shows `64 plain` (the salt's length
  in hex digits, and how the boot classifies the disk).
* [ ] **Extra disk:** plug in another USB stick or disk (any contents) next to the boot stick and boot
  the decoy. It still boots with one password; record how long the step after the password takes.
* [ ] **Keymap** (only if you set `keymap`): a password with layout-specific characters works.

### B13. Crash recovery

Boot the hidden side and log in, then:
```sh
dd if=/dev/urandom of=/root/crash bs=1M count=2000 &
```
While it runs, hold the power button until the machine turns off. Boot the hidden side again.

* [ ] It boots normally (the boot environment never replays the filesystem log; Arch does).
* [ ] `dmesg | grep -i -E 'btrfs.*(error|corrupt)'` shows nothing.

### B14. Maintenance from the setup environment

Boot the setup stick, then mount it (B2) and run:
```sh
pd open --config /m/pd.toml                         # decoy, read-only; asks for the password
cat /mnt/pd/arch/etc/hostname                       # testdecoy
pd close
pd open --config /m/pd.toml --offset NNNNNNNNNN     # hidden
cat /mnt/pd/arch/etc/hostname                       # testhidden
pd close
```

* [ ] Both open read-only and close cleanly (`pd close` prints `Closed.`).

---

## C. YubiKey (FIDO2)

This uses the disk from part B. Each YubiKey must have a FIDO2 PIN.

> [!WARNING]
> A YubiKey refuses PINs after 3 wrong attempts in a row until it's unplugged and plugged in again,
> and locks for good after 8 wrong attempts in total (a correct PIN resets the count). Unlocking
> means a FIDO reset, which erases its credentials, and with them access to both test sides. Don't
> enter more wrong PINs than a step asks for.

### C1. Set the PIN, if the key has none

On any machine with `ykman`: `ykman fido access change-pin`. Or in the setup environment (works only
while no PIN is set):
```sh
fido2-hmac list                         # e.g. /dev/hidraw0
echo 'your-test-pin' | fido2-hmac setpin /dev/hidraw0
```
Use at least 6 characters.

### C2. Enroll the key for both sides

Boot the setup stick, mount it (B2), plug in the YubiKey, then:
```sh
sed -i '1i fido2 = "fido2.txt"' /m/pd.toml
fido2-hmac list                                     # shows the key: /dev/hidrawN
pd fido2 enroll --config /m/pd.toml                 # decoy password, PIN, touch the key when it blinks
```
* [ ] It asks `Plug in the next security key and press Enter, or type "done":`. Type `done` (or, with
  a second key, plug it in and press Enter, then enter its PIN and touch it).
* [ ] It prints `Enrolled 1 security key(s) for this side; the password alone no longer opens it.`

```sh
pd fido2 enroll --config /m/pd.toml --offset NNNNNNNNNN    # hidden password, PIN, touch; then done
pd fido2 list --config /m/pd.toml
```
* [ ] The list shows `0: …  plugged in: /dev/hidrawN` (one line per key).

### C3. Rewrite the boot USB

In the same setup session, plug in the boot stick and:
```sh
lsblk -o NAME,SIZE,MODEL,TRAN                      # the boot stick's partition, e.g. /dev/sdb1
mkdir -p /b && mount /dev/sdb1 /b
pd usb --output /b --config /m/pd.toml
umount /b
sync; umount /m; poweroff -f
```
* [ ] It prints `wrote EFI/BOOT/BOOTX64.EFI` and `wrote EFI/BOOT/BOOTX64.EFI.extra.d/fido2.cred`.

### C4. Boot with the key

With the YubiKey plugged in, boot the decoy.

* [ ] After the password: `Security key PIN:`, then `Touch your security key.` and the key blinks.
  After the touch, the decoy boots.
* [ ] Boot the hidden side the same way (B11, then the PIN and a touch).

### C5. Failure cases (decoy)

* [ ] **One wrong PIN:** `Wrong PIN.` and the PIN prompt again; the right PIN boots.
* [ ] **Key plugged in late:** start without the key. After the PIN it prints
  `Insert your security key.`; plug it in within a minute, touch it, and it boots.
* [ ] **No key:** start without the key and don't plug it in. After a minute: `No security key found.`,
  then `No bootable disk found.` (the password alone no longer opens the decoy).
* [ ] **No touch:** enter the PIN but don't touch. After 15–30 seconds: `Not touched.` and the PIN
  prompt again; this time touch it, and it boots.
* [ ] **Optional, uses 3 attempts:** three wrong PINs in a row print
  `PIN blocked: remove and reinsert the security key.` Unplug and replug it, boot again with the right
  PIN.

### C6. Change a password with the key enrolled

Boot the setup stick, mount it, plug in the key:
```sh
pd rekey --config /m/pd.toml                        # current and new decoy password, PIN, touch, done
```
* [ ] `Re-keyed.` and a note to rebuild the boot USB. Rebuild it (C3), then boot the decoy with the
  new password, the PIN and a touch.

---

## D. Stock boot

This erases the part B and C setup. Follow [stock-boot.md](stock-boot.md); the steps below are the
test-specific parts.

### D1. Randomize the disk

Boot the setup stick, mount it (B2), and run `pd randomize --config /m/pd.toml`. Power off.

### D2a. Decoy option A: Fedora's installer (tests a real installer's layout)

Boot the Fedora installer stick and install Fedora onto the target disk with:
* custom partitioning with the **"LVM Thin Provisioning"** scheme (in the classic installer's custom
  partitioning screen; with the newer web installer, use its storage editor to create a thin pool);
* **encryption** enabled (LUKS2);
* **XFS** (or btrfs) for the thin volumes, not ext4.

Then boot Fedora and work through stock-boot.md's [discard checklist](stock-boot.md#discards).
Fedora specifics to expect:
* `/etc/crypttab` probably has `discard`; remove it, then `sudo dracut -f --regenerate-all`.
* `fstrim.timer` is enabled; disable it and install the shutdown unit.

Reboot, then record:
```sh
lsblk --discard                     # DISC-MAX 0 for the luks-… mapping
sudo lvs -a -o lv_name,lv_size,seg_pe_ranges,chunk_size,zero
sudo vgs                            # VFree
sudo cryptsetup luksUUID /dev/nvme0n1p3    # the LUKS partition; check with lsblk
```

### D2b. Decoy option B: by hand, with Arch and systemd-boot

From the setup environment (B1–B3), after D1:
```sh
d=/dev/nvme0n1 p1=/dev/nvme0n1p1 p2=/dev/nvme0n1p2   # on a SATA disk: /dev/sda, /dev/sda1, /dev/sda2
printf 'label: gpt\nsize=1GiB, type=U\ntype=L\n' | sfdisk $d
mkfs.vfat -F 32 $p1
cryptsetup luksFormat $p2                          # the decoy's LUKS passphrase
cryptsetup open $p2 cryptroot
lvm pvcreate /dev/mapper/cryptroot
lvm vgcreate vg0 /dev/mapper/cryptroot
lvm lvcreate -L 8G -n swap vg0
lvm lvcreate --type thin-pool -l 100%FREE --discards nopassdown -Zn -n pool vg0
lvm lvcreate -V 200G -T vg0/pool -n root            # any virtual size
mkfs.xfs /dev/vg0/root; mkswap /dev/vg0/swap
mkdir -p /mnt/decoy && mount -o nodiscard /dev/vg0/root /mnt/decoy
mkdir /mnt/decoy/boot && mount $p1 /mnt/decoy/boot
r=/mnt/decoy
pacstrap -K $r base linux linux-firmware lvm2 cryptsetup xfsprogs efibootmgr vim
genfstab -U $r | sed -E 's/\txfs\t([^\t]*)/\txfs\t\1,nodiscard/' >> $r/etc/fstab
echo '/dev/vg0/swap none swap defaults 0 0' >> $r/etc/fstab
echo stockdecoy > $r/etc/hostname
sed -i 's/^HOOKS=.*/HOOKS=(base udev autodetect microcode modconf kms keyboard keymap consolefont block encrypt lvm2 filesystems fsck)/' \
    $r/etc/mkinitcpio.conf
install -Dm644 $PD_SRC/target/arch/fstrim-before-umount.service $r/etc/systemd/system/
uuid=$(cryptsetup luksUUID $p2)
arch-chroot $r sh -c "bootctl install && mkinitcpio -P && systemctl enable fstrim-before-umount && passwd"
cat > $r/boot/loader/entries/arch.conf <<EOF
title Arch Linux
linux /vmlinuz-linux
initrd /initramfs-linux.img
options cryptdevice=UUID=$uuid:cryptroot root=/dev/vg0/root rw
EOF
umount $r/boot $r; lvm vgchange -an vg0; cryptsetup close cryptroot
```
(The unit file is pd's, but its contents are a generic shutdown trim; stock-boot.md has the same text.)

* [ ] Remove the setup stick and reboot: systemd-boot starts, asks for the LUKS passphrase, and Arch
  boots to `stockdecoy login:`. Record `lsblk --discard` (DISC-MAX 0 for cryptroot).

### D3. Add the hidden side

Boot the setup stick, mount it (B2), run `pd-net`, and write `/m/stock.toml`: a copy of `pd.toml`
with the decoy's LUKS UUID as the salt.
```sh
uuid=$(cryptsetup luksUUID /dev/nvme0n1p2)          # p3 with Fedora
{ echo "salt = \"$uuid\""; sed '/^fido2 = /d' /m/pd.toml; } > /m/stock.toml
pd init-hidden --config /m/stock.toml --stock-decoy /dev/nvme0n1p2 --run
```

* [ ] It asks for the decoy's LUKS passphrase and the hidden password (twice), and opens a shell.
  If it refuses instead (discards allowed, or a thin pool that isn't one segment), record the message:
  that's a finding about the installer's layout.

Install Arch in that shell exactly as in B5 (with `testhidden`), then `exit`.

* [ ] It prints `Hidden boot from a live system, as root (docs/stock-boot.md). The salt is the decoy's
  LUKS UUID, read back at boot.` and four commands. **Copy them down exactly.**

Power off (`sync; umount /m; poweroff -f`).

### D4. The decoy still works

Boot the decoy normally (its own bootloader, no USB stick).

* [ ] It boots, and its files are as before.

### D5. Hidden boot from Debian Live

Boot the Debian Live stick and choose the live system in its menu. Open a terminal (or switch to a
console with Ctrl+Alt+F2 and log in as `user`, password `live`), then run `sudo -i` and type the four
commands from D3. After `read -rs P`, type the hidden password and press Enter (nothing echoes).

* [ ] `Free space between the two sides: N MiB.` and the hidden Arch boots to `testhidden login:`.
* [ ] Record the time between the password and that line (Argon2 runs on one core here: about a
  second).

### D6. Hidden boot from Tails

Boot the Tails stick. At the Welcome Screen, open the additional settings, set an **Administration
Password**, and start Tails. Open a terminal, run `sudo -i` (enter the administration password), and
type the four commands.

* [ ] The hidden Arch boots, as in D5. If anything differs from Debian Live (missing commands, a busy
  disk, a kexec error), record it.

### D7. Leftovers

Boot the decoy and run `sudo efibootmgr`.

* [ ] Record any boot entries the firmware added for the live sticks (stock-boot.md explains how to
  delete them).

---

## F. Optional: compaction and moving

A rehearsal, on the test disk, of adding a hidden side to an install whose decoy was once nearly full
(as for a migrated v1 install), and of moving to another disk.

### F1. A decoy that was once full

Boot the setup stick, mount it (B2), run `pd-net`, then randomize and create a decoy only (B4, B5).
Before leaving B5's shell, fill and mostly empty the decoy:
```sh
mkdir $PD_ROOT/arch/fill
n=$(( $(df -B1G --output=avail $PD_ROOT/arch | tail -1) * 9 / 10 ))       # GiB to write: 90% of the free space
for i in $(seq $n); do head -c 1G /dev/urandom > $PD_ROOT/arch/fill/f$i; done; sync
for i in $(seq $n); do [ $((i % 6)) -eq 0 ] || rm $PD_ROOT/arch/fill/f$i; done; sync
(cd $PD_ROOT/arch/fill && sha256sum f* > /m/sums.txt)
exit
```
(Writing that much takes a while; a smaller test disk is quicker.)

### F2. Compact, then add the hidden side

```sh
pd init-hidden --config /m/pd.toml          # expected: refuses, "Run `pd compact` on the decoy first"
pd compact --config /m/pd.toml              # expected: refuses, needs --no-hidden or --hidden-offset
pd compact --config /m/pd.toml --no-hidden  # decoy password; prints how far the data moved, then refills
pd open --config /m/pd.toml --run 'cd arch/fill && sha256sum -c --quiet /m/sums.txt && echo SUMS-OK'
pd init-hidden --config /m/pd.toml --run    # now creates the hidden side; install Arch as in B6
```
* [ ] `pd compact` reports the highest block before and after, and the boundary, and "refilling N MiB
  of free space above the boundary"; `SUMS-OK`.
* [ ] If the machine was rebooted since the decoy was created, `pd init-hidden` prints "rewriting the
  pool's metadata" and "refilling" before it creates the hidden side (in the same boot it doesn't).
* [ ] Record how long `pd compact` took and how much it moved.
* [ ] Boot both sides from the boot stick (B9, B11). The frontier monitor shows the free space.

### F3. Move to another disk (only with a second disk, e.g. in a USB enclosure)

```sh
pd move --config /m/pd.toml --to /dev/sdX --hidden-offset NNNNNNNNNN      # /dev/sdX: the second disk; erased
```
* [ ] It prints `Moved.` and the hidden side's new offset. Write it down.
* [ ] With the second disk as the only internal disk, boot both sides from the same boot stick.

---

## E. Report

Send back the results table, plus for every failure: the step, the exact message (or a photo), and
what you tried. Useful extras:
* from a booted target OS: `journalctl -b > /root/journal.txt` and `dmesg > /root/dmesg.txt` (copy
  them off with a USB stick);
* from pd's recovery shell: `dmesg`, `ls /sys/block`, `ls /dev/hidraw*` (photos are fine).

When you're done, wipe the setup stick: it holds `pd.toml` and `fido2.txt`.

### Results

| Item | Result |
|---|---|
| Machine, firmware version | |
| Disk (model, size), storage mode | |
| RAM; NIC | |
| B1 setup environment: display, keyboard, disk, NIC | |
| B3 `pd-net` | |
| B4 randomize time | |
| B5/B6 installs (`pacstrap` signature checks, `mkinitcpio`) | |
| B9 decoy boot; `lsblk --discard`; swap; `efibootmgr` | |
| B10 shutdown trim (data% before / after file / after reboot) | |
| B11 hidden boot; frontier message | |
| B12 menu; wrong password; Argon2 time; keymap | |
| B13 crash recovery | |
| B14 `pd open` / `pd close` | |
| B7 NixOS (optional) | |
| C2 enrollment (both sides) | |
| C4 boot with the key (both sides) | |
| C5 wrong PIN; late key; no key; no touch; PIN blocked | |
| C6 rekey | |
| D2 decoy (Fedora or manual); layout (`lvs -a` output) | |
| D3 `init-hidden --stock-decoy` | |
| D4 decoy after D3 | |
| D5 Debian Live hidden boot; Argon2 time | |
| D6 Tails hidden boot | |
| D7 firmware entries | |
| F2 compaction (moved MiB, time); boots | |
| F3 move; boots from the new disk | |
