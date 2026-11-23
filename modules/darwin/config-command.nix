{
  config,
  configurationName,
  lib,
  pkgs,
  ...
}: let
  dotfilesNixDir = config.home-manager.users.${config.system.primaryUser}.home.checkouts.dotfiles-nix.path;
  configCommand = import ../config-command.nix {
    inherit lib pkgs dotfilesNixDir;
    manager = "nix-darwin";
    managerCmd = "${config.system.build.darwin-rebuild}/bin/darwin-rebuild";
  };
in {
  options.programs.config = configCommand.options;

  config = {
    programs.config.flake = lib.mkDefault "${dotfilesNixDir}#${configurationName}";
    environment.systemPackages = [
      (configCommand.package config.programs.config)
    ];
  };
}
