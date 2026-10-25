# Booting

This describes v2; v1 differences are noted. After you press Enter at the password prompt, Argon2 takes
several seconds. That pause is expected.

## Decoy boot

1. Insert the USB and choose it in the firmware's boot menu. The UKI is at `EFI/BOOT/BOOTX64.EFI`, the
   removable-media path, so no boot entry needs to exist beforehand.
2. At `Password:`, type the decoy password and press Enter.
3. The default system boots.
   * To choose a different system, hold **Shift** after pressing Enter until a menu appears.
   * For NixOS, keep holding **Shift** to get the generation menu.

## Hidden boot

1. Boot the USB as above.
2. At `Password:`, press **Enter** with no password. After the Argon2 pause, no disk opens and you land
   in the recovery shell. (Planned for v2: pressing Ctrl-C at the prompt skips the pause.)
3. Type:
   ```sh
   sed -i s/2048/<offset>/ /early_env.sh
   /early.sh
   ```
4. At `Password:`, type the hidden password.

`<offset>` is the start sector of the hidden plain volume. Setup prints it once. Memorize it, and never
write it on the USB or anywhere in the decoy.

**v1** uses different commands. They also apply the hidden salt suffix, which v2 removes:
```sh
sed -i s/2048/<offset>/ /table.txt
echo a >> /salt.txt
/early.sh
```

The edits only change the in-RAM copy of the boot environment. The USB is never written to.

## Recovery shell

* It's a normal shell. Typing `exit` powers the machine off.
* If the right password doesn't work, check that:
  * the right disk is attached;
  * the offset was typed correctly (hidden boot only);
  * you typed the password for the US keyboard layout, which the boot environment uses.
* **After an unclean shutdown**, v2's boot environment mounts btrfs with `rescue=nologreplay`, and the OS
  replays its own log once it's running.
  * v1 lacks this. Depending on the kernel, a v1 boot right after a crash either fails at the `@boot`
    mount, or replays the log despite the read-only intent.
  * If a v1 boot fails that way, mount the root read-write once from any environment that can open the
    disk, to replay the log, then boot again.

## Rebuilding the USB

The installed OSes update their own kernels; the USB never needs to know about it. Rebuild the USB only
to update the boot environment itself.

* **v2:** `pd usb` reads the salt back from the existing USB.
* **v1:** set `STATIC_SALT`/`SALT` to the original salt and rerun the UKI build ([v1.md](v1.md)).
