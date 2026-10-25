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
        pdPyz = pd.pdPyz;
        inherit (pd) testenv bootTools plainTools bootInitrd plainTree modulesClosure detectShift fido2Hmac kernel;
        default = pkgs.linkFarm "pd" [
          { name = "bzImage"; path = pd.kernelImage; }
          { name = "boot-initrd.cpio.zst"; path = pd.bootInitrd; }
          { name = "plain"; path = pd.plainTree; }
          { name = "stub.efi"; path = pd.stub; }
          { name = "ovmf"; path = pd.ovmf; }
        ];
      };

      nixosModules.default = ./target/nixos/pd.nix;
    };
}
