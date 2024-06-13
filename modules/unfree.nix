# Modified from
#   https://github.com/NixOS/nixpkgs/issues/197325#issuecomment-1579420085
# to allow passing a package instead of its name.
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
    allowed = lib.map lib.getName config.allowUnfreePackages;
  in {
    nixpkgs.config.allowUnfreePredicate = pkg: builtins.elem (lib.getName pkg) allowed;
  };
}
