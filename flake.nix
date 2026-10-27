{
  description = "Plain Disguise (pd): plausible-deniability boot setup";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      pd = import ./nix { inherit pkgs; src = self; };
    in
    {
      packages.${system} = {
        nixosTest = import ./nix/nixos-test.nix { inherit nixpkgs system; src = self; };
        setupUki = pd.setupEnv.uki;
        # The release boot image (docs/design.md §7.1).
        bootUki = pd.bootUki;
        pdPyz = pd.pdPyz;
        setupInitrd = pd.setupEnv.initrd;
        # What a release publishes (docs/release.md).
        inherit (pd) release bootToolsTarball;
        inherit (pd) testenv bootTools plainTools bootInitrd plainTree modulesClosure detectShift fido2Hmac kernel;
        default = pkgs.linkFarm "pd" [
          { name = "bzImage"; path = pd.kernelImage; }
          { name = "boot-initrd.cpio.zst"; path = pd.bootInitrd; }
          { name = "plain"; path = pd.plainTree; }
          { name = "stub.efi"; path = pd.stub; }
          { name = "ovmf"; path = pd.ovmf; }
        ];
      };

      # For running the tests: Python with pexpect, and QEMU (the CanoKey build
      # the FIDO2 tests use, so the same one runs everything).
      devShells.${system}.default = pkgs.mkShell {
        packages = [ (pkgs.python3.withPackages (ps: [ ps.pexpect ])) pd.qemuCanokey ];
      };

      nixosModules.default = ./target/nixos/pd.nix;
    };
}
