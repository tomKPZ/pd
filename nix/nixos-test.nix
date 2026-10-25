{ nixpkgs, system, src }:
# A minimal NixOS system used by tests/test_nixos.py: its kernel and initrd
# are installed into the fake target root, so the test exercises the real
# NixOS initrd with the pd module.
let
  nixos = nixpkgs.lib.nixosSystem {
    inherit system;
    modules = [
      (src + "/target/nixos/pd.nix")
      ({ ... }: {
        pd.enable = true;
        # Exercise the combined script; the v1 branch is skipped without header.dat.
        pd.v1Compat = true;
        fileSystems."/" = {
          device = "/dev/sysvg/root";
          fsType = "btrfs";
          options = [ "subvol=@" "noatime" "nodiscard" ];
        };
        boot.initrd.availableKernelModules = [ "virtio_pci" "virtio_blk" "nvme" ];
        boot.kernelParams = [ "console=ttyS0" ];
        system.stateVersion = "26.05";
      })
    ];
  };
  cfg = nixos.config;
in
nixpkgs.legacyPackages.${system}.linkFarm "pd-nixos-test" [
  { name = "kernel"; path = "${cfg.system.build.kernel}/${cfg.system.boot.loader.kernelFile}"; }
  { name = "initrd"; path = "${cfg.system.build.initialRamdisk}/${cfg.system.boot.loader.initrdFile}"; }
]

