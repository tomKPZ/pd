{
  description = "pd: plausible-deniability boot setup";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      pd = import ./nix { inherit pkgs; src = self; };
    in
    {
      packages.${system} = {
        inherit (pd) testenv bootTools plainTools bootInitrd plainTree modulesClosure detectShift kernel;
        default = pkgs.linkFarm "pd" [
          { name = "bzImage"; path = pd.kernelImage; }
          { name = "boot-initrd.cpio.zst"; path = pd.bootInitrd; }
          { name = "plain"; path = pd.plainTree; }
          { name = "stub.efi"; path = pd.stub; }
          { name = "ovmf"; path = pd.ovmf; }
        ];
      };
    };
}
