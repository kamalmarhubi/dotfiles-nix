# Modified from
#   https://github.com/NixOS/nixpkgs/issues/197325#issuecomment-1579420085
# to allow passing a package instead of its name, and with further
# modifications with help from Claude to collect unfree packges from
# home-manager configs when using the home-manager nixos or darwin module.
{
  lib,
  config,
  # osConfig is passed in when home-manager is being used as a module under
  # nixos or nix-darwin; it contains the whole config for the system.
  osConfig ? null,
  ...
}: {
  options = with lib; {
    nixpkgs.allowUnfreePackages = mkOption {
      type = types.listOf types.package;
      default = [];
    };
  };
  config = let
    # We are at the system level if home-manager is an attribute on config.
    atSystemLevel = config ? home-manager;
    # Find the nixos / nix-darwin home-manager module config;
    # depends on context for this module's evaluation:
    # - top-level context: it will be under config.home-manager
    # - home-manager context via nixos / nix-darwin module: it will be under
    #   osConfig.home-manager
    #
    # When running without nixos or nix-darwin, there is no config to find.
    homeManagerConfig = config.home-manager or osConfig.home-manager or null;
    usingGlobalPkgs = homeManagerConfig.useGlobalPkgs or false;

    # Predicate for allowUnfreePackages at the top-level (could be stand-alone
    # home-manager or system-level packages on nixos / nix-darwin).
    topLevelPredicate = pkg:
      builtins.elem (lib.getName pkg) (lib.map lib.getName config.nixpkgs.allowUnfreePackages);

    # Predicate for home-manager user packages collected from
    #   home-manager.users.<user>.nixpkgs.allowUnfreePackages
    collectedUserPackagesPredicate = pkg:
      let
        homeManagerUnfree = lib.flatten (lib.mapAttrsToList
          (_: userConfig: userConfig.nixpkgs.allowUnfreePackages or [])
          homeManagerConfig.users);
        allowed = lib.map lib.getName homeManagerUnfree;
      in
        builtins.elem (lib.getName pkg) allowed;
  in
    # Avoid setting the predicate in both nixos / nix-darwin and home-manager
    # when using useGlobalPkgs.
    lib.mkIf (atSystemLevel || !usingGlobalPkgs) {
      nixpkgs.config.allowUnfreePredicate = pkg:
        topLevelPredicate pkg || (usingGlobalPkgs && collectedUserPackagesPredicate pkg);
    };
}
