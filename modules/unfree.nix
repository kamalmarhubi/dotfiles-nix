# Modified from
#   https://github.com/NixOS/nixpkgs/issues/197325#issuecomment-1579420085
# to allow passing a package instead of its name, and with further
# modifications with help from Claude to collect unfree packges from
# home-manager configs when using the home-manager nixos or darwin module.
{
  lib,
  config,
  ...
}: {
  options = with lib; {
    nixpkgs.allowUnfreePackages = mkOption {
      type = types.listOf types.package;
      default = [];
    };
  };
  config = let
    hasHomeManagerConfig = config ? home-manager;
  in
    lib.mkMerge [
      (lib.mkIf hasHomeManagerConfig {
        nixpkgs.config.allowUnfreePredicate = let
          topLevelUnfree = config.nixpkgs.allowUnfreePackages;
          homeManagerUnfree = lib.flatten (lib.mapAttrsToList
            (_: userConfig: userConfig.nixpkgs.allowUnfreePackages or [])
            config.home-manager.users);
          allUnfree = topLevelUnfree ++ homeManagerUnfree;
          allowed = lib.map lib.getName allUnfree;
        in
          pkg: builtins.elem (lib.getName pkg) allowed;
      })
      (lib.mkIf (!hasHomeManagerConfig) {
        # Assumed to be standalone home-manager config.
        nixpkgs.config.allowUnfreePredicate = let
          allowed = lib.map lib.getName config.nixpkgs.allowUnfreePackages;
        in
          pkg: builtins.elem (lib.getName pkg) allowed;
      })
    ];
}
