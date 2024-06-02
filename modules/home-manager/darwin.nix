{
  config,
  lib,
  pkgs,
  ...
}:
with lib; {
  # Disable built-in symlinking of the Home Manager Applications directory.
  disabledModules = ["targets/darwin/linkapps.nix"];
  # Instead, create "trampoline apps" using a script from
  #   https://github.com/nix-community/home-manager/issues/1341#issuecomment-1870352014
  # and adapting the module content to directly place trampolines in
  #   ~/Applications/Home Manager Apps
  config = mkIf pkgs.stdenv.hostPlatform.isDarwin {
    home.extraActivationPath = with pkgs; [
      rsync
      dockutil
      gawk
    ];
    launchd.agents.setPath = let
      setPath = pkgs.writeShellApplication {
        name = "set-path";
        text = ''
          path_from_system="$(unset PATH; eval "$(/usr/libexec/path_helper -s)"; echo "$PATH")"
          uid="$(id -u)"

          /bin/launchctl setenv PATH "${config.home.profileDirectory}/bin:/run/current-system/sw/bin:$path_from_system"

          /bin/launchctl kickstart -k "gui/$uid/com.apple.Finder"
          /bin/launchctl kickstart -k "gui/$uid/com.apple.Spotlight"
          /bin/launchctl kickstart -k "gui/$uid/com.apple.Dock.agent"
        '';
      };
    in {
      enable = true;
      config = {
        Program = "${setPath}/bin/set-path";
        RunAtLoad = true;
      };
    };
    home.activation.trampolineApps = let
      apps = pkgs.buildEnv {
        name = "home-manager-applications";
        paths = config.home.packages;
        pathsToLink = "/Applications";
      };
    in
      hm.dag.entryAfter ["writeBoundary"] ''
        ${builtins.readFile ./lib-bash/trampoline-apps.sh}
        fromDir="${apps}/Applications"
        toDir="$HOME/Applications/Home Manager Apps"
        sync_trampolines "$fromDir" "$toDir"
      '';
  };
}
