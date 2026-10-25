# NixOS integration for pd.  Import this module in the configuration of
# every NixOS system installed on a pd disk (decoy or hidden alike).
#
# pd's boot environment kexecs into this system's kernel and appends
# /boot_tmp/{data.key,table.txt,crypt.env} to its initrd.  The pd-unlock unit
# below finds the disk, opens it as /dev/mapper/cryptlvm and destroys the
# key; LVM activation and the root mount then proceed as usual.
{ config, lib, pkgs, ... }:
let
  cfg = config.pd;
in
{
  options.pd = {
    enable = lib.mkEnableOption "booting from a pd disk";
    v1Compat = lib.mkEnableOption ''
      also accepting keys from a v1 boot USB (detached LUKS header in
      /boot_tmp/header.dat), for the transition described in
      docs/migration.md.  Remove the system's own v1 pd-unlock unit when
      enabling this
    '';
  };

  config = lib.mkIf cfg.enable {
    services.lvm.boot.thin.enable = true;

    boot.initrd.systemd.enable = true;
    boot.initrd.kernelModules = [ "dm-mod" "dm-crypt" ];
    # Pulls in the crypto modules dm-crypt needs.
    boot.initrd.luks.forceLuksSupportInInitrd = true;

    boot.initrd.systemd.services.pd-unlock = {
      description = "Unlock the pd disk";
      requiredBy = [ "initrd.target" ];
      wants = [ "systemd-udev-trigger.service" "systemd-modules-load.service" ];
      after = [ "systemd-udev-trigger.service" "systemd-modules-load.service" ];
      before = [ "cryptsetup.target" "initrd-root-device.target" "initrd-root-fs.target" ];
      unitConfig.DefaultDependencies = false;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      path = [
        pkgs.coreutils
        pkgs.gnused
        pkgs.cryptsetup
        config.services.lvm.package
        config.boot.initrd.systemd.package
      ];
      script = ''
        # Coldplug must enqueue devices before settling.
        udevadm settle --timeout=60
      '' + lib.optionalString cfg.v1Compat ''
        if [ -e /boot_tmp/header.dat ]; then
          test -b /boot_tmp/disk
          dmsetup create dm-luks /boot_tmp/table.txt
          rm -f /boot_tmp/disk
          cryptsetup open --key-file=/boot_tmp/key.bin --keyfile-offset=64 \
            --header=/boot_tmp/header.dat /dev/mapper/dm-luks luks
          shred -u /boot_tmp/*
          rmdir /boot_tmp
          exit 0
        fi
      '' + builtins.readFile ../hook.sh;
    };

    # pd's boot environment reads the kernel, initrd and kernel parameters of
    # each generation from /boot/<generation>/.
    boot.loader.grub.enable = false;
    boot.loader.generationsDir.enable = true;
  };
}

