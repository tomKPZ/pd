{ config
, lib
, pkgs
, ...
}: {
  imports = [
    ./hardware-configuration.nix
    ./filesystems.nix
  ];

  services.lvm.boot.thin.enable = true;

  boot = {
    initrd.luks.forceLuksSupportInInitrd = true;

    initrd.preLVMCommands = ''
      udevadm settle
      dmsetup create dm-luks /boot_tmp/table.txt
      rm -f /boot_tmp/disk
      cryptsetup open --key-file=/boot_tmp/key.bin --keyfile-offset=64 --header=/boot_tmp/header.dat /dev/mapper/dm-luks luks
      shred -u /boot_tmp/*
      rmdir /boot_tmp
    '';
    loader.grub = {
      enable = false;
    };
    loader.generationsDir = {
      enable = true;
    };
    supportedFilesystems = [ "btrfs" ];
  };

  networking.hostName = "nixos";
  networking.networkmanager.enable = true;

  time.timeZone = "America/Los_Angeles";

  i18n.defaultLocale = "en_US.UTF-8";
  console = {
    font = "Lat2-Terminus16";
    keyMap = "us";
  };

  environment.systemPackages = with pkgs; [
    vim
    wget
  ];

  system.stateVersion = "25.05";
}
