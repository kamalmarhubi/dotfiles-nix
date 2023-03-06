{
  config,
  pkgs,
  lib,
  inputs,
  system,
  ...
}: {
  # Disable built-in symlinking of the Home Manager Applications directory.
  disabledModules = ["targets/darwin/linkapps.nix"];
  # Add a custom mkalias based thing cribbed from:
  #   https://github.com/nix-community/home-manager/issues/1341#issuecomment-1446696577
  # but using mkalias as in
  #   https://github.com/reckenrode/nixos-configs/commit/26cf5746b7847ec983f460891e500ca67aaef932?diff=unified
  # instead; latter found via
  # via
  #   https://github.com/nix-community/home-manager/issues/1341#issuecomment-1452420124
  home.activation.aliasApplications =
    lib.mkIf pkgs.stdenv.hostPlatform.isDarwin
    (let
      apps = pkgs.buildEnv {
        name = "home-manager-applications";
        paths = config.home.packages;
        pathsToLink = "/Applications";
      };
    in
      lib.hm.dag.entryAfter ["linkGeneration"] ''
        app_path="$HOME/Applications/Home Manager Apps"
        tmp_path="$(mktemp -dt "home-manager-apps.XXXXXXXXXX")" || exit 1

        for app in \
          $(find "${apps}/Applications" -maxdepth 1 -type l)
        do
          real_app="$(realpath "$app")"
          app_name="$(basename "$app")"
          $DRY_RUN_CMD ${inputs.mkalias.outputs.apps.${system}.default.program} "$real_app" "$tmp_path/$app_name"
        done

        $DRY_RUN_CMD rm -rf "$app_path"
        $DRY_RUN_CMD mv "$tmp_path" "$app_path"
      '');
}
