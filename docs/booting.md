# Booting

This describes v2; v1 differences are noted. After you press Enter at the password prompt, Argon2 takes
several seconds. That pause is expected.

## Decoy boot

1. Insert the USB and choose it in the firmware's boot menu. The UKI is at `EFI/BOOT/BOOTX64.EFI`, the
   removable-media path, so no boot entry needs to exist beforehand.
2. At `Password:`, type the decoy password and press Enter.
   * With FIDO2 security keys: at `Security key PIN:`, type the PIN, plug in an enrolled key if it
     isn't already (the boot waits up to a minute), and touch it when asked.
3. The default system boots.
   * To choose a different system, hold **Shift** after pressing Enter until a menu appears.
   * For NixOS, keep holding **Shift** to get the generation menu.

## Hidden boot

1. Boot the USB as above.
2. At `Password:`, press **Enter** with no password. You land in the recovery shell immediately. (v2
   skips Argon2 for an empty password explicitly; in v1, `argon2` refuses an empty password and
   `set -euo pipefail` ends `early.sh`.)
3. Type:
   ```sh
   sed -i s/2048/<offset>/ /early_env.sh
   /early.sh
   ```
4. At `Password:`, type the hidden password (then the PIN and a touch, with FIDO2). Before handing over, the hidden side's frontier monitor
   prints `Free space between the two sides: N MiB.`; if it warns (little space left, or a decoy volume
   inside the hidden region), it waits for Enter.

`<offset>` is the start sector of the hidden plain volume. Setup prints it once; by default it's a
multiple of 1,000,000 (e.g. `1945000000`). Remember it, and never write it on the USB or anywhere in the
decoy. It isn't secret ([threat-model.md](threat-model.md#a2-coercer-in-scope-the-main-design-target)),
but a written note of it is a pointer to the hidden side.

**v1** uses different commands. They also apply the hidden salt suffix, which v2 removes:
```sh
sed -i s/2048/<offset>/ /table.txt
echo a >> /salt.txt
/early.sh
```

The edits only change the in-RAM copy of the boot environment. The USB is never written to.

With [stock boot](stock-boot.md) there's no pd USB: the decoy boots on its own, and the hidden side
is booted from a live system ([stock-boot.md §4](stock-boot.md#4-booting)).

## Recovery shell

* It's a normal shell. Typing `exit` powers the machine off.
* If the right password doesn't work, check that:
  * the right disk is attached;
  * the offset was typed correctly (hidden boot only);
  * you typed the password for the boot environment's keyboard layout: US unless `keymap` was set
    when the USB was built.
* **After an unclean shutdown**, v2's boot environment mounts btrfs with `rescue=nologreplay`, and the OS
  replays its own log once it's running. (v1 lacks this; because it activates its LVs read-write, its
  `ro` mount replays the log and writes to the disk; see [design.md §7.3](design.md#73-latesh-in-the-plain-volume).)
* **The shutdown trim** is skipped after a crash; the next clean shutdown catches up.
* **FIDO2:** `Wrong PIN.` asks again (three times). After three wrong PINs in a row the key refuses
  until it's unplugged and plugged in again; after eight in total it locks for good. `No security key
  found.` means no plugged-in key holds one of the USB's credentials.

## Rebuilding the USB

The installed OSes update their own kernels; the USB never needs to know about it. Rebuild the USB only
to update the boot environment itself.

* **v2:** `pd usb --salt-from OLD.EFI` takes the salt (and `fido2.txt`, if the config has none) from the existing UKI ([install.md](install.md#3-build-the-boot-usb)).
* **v1:** set `STATIC_SALT`/`SALT` to the original salt and rerun the UKI build ([v1.md](v1.md)).
