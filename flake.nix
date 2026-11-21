# AnvilDroid for NixOS.
#
# The package is the prebuilt release (packaging/nixos/sources.json pins the
# release asset and its sha256), so installing it compiles nothing.
#
#   inputs.anvildroid = {
#     url = "github:anvildroid/anvildroid";
#     inputs.nixpkgs.follows = "nixpkgs";
#   };
#
#   nixosConfigurations.<host> = nixpkgs.lib.nixosSystem {
#     modules = [
#       anvildroid.nixosModules.default
#       { programs.anvildroid.enable = true; }
#     ];
#   };
#
# See packaging/nixos/README.md.
{
  description = "AnvilDroid: isolated Android devices and apps as native windows, on Waydroid";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in
    {
      packages.${system} = {
        anvildroid = pkgs.callPackage ./packaging/nixos/package.nix { };
        default = self.packages.${system}.anvildroid;
      };

      overlays.default = final: _prev: {
        anvildroid = final.callPackage ./packaging/nixos/package.nix { };
      };

      nixosModules = {
        anvildroid =
          { pkgs, lib, ... }:
          {
            imports = [ ./packaging/nixos/module.nix ];
            programs.anvildroid.package = lib.mkDefault (pkgs.callPackage ./packaging/nixos/package.nix { });
          };
        default = self.nixosModules.anvildroid;
      };

      checks.${system} = {
        package = self.packages.${system}.anvildroid;
        vm = import ./packaging/nixos/test.nix { inherit self pkgs; };
        module =
          (nixpkgs.lib.nixosSystem {
            inherit system;
            modules = [
              self.nixosModules.default
              {
                programs.anvildroid.enable = true;
                system.stateVersion = "25.11";
                fileSystems."/" = {
                  device = "/dev/sda1";
                  fsType = "ext4";
                };
                boot.loader.grub.devices = [ "/dev/sda" ];
              }
            ];
          }).config.system.build.toplevel;
      };
    };
}
