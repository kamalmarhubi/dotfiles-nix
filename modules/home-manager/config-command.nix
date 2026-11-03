{
  config,
  configurationName,
  dotfilesNixDir,
  lib,
  pkgs,
  ...
}: let
  configCommand = import ../config-command.nix {
    inherit lib pkgs dotfilesNixDir;
    manager = "home-manager";
    managerCmd = "${config.programs.home-manager.package}/bin/home-manager";
  };
in {
  options.programs.config = configCommand.options;

  config = {
    programs.config.flake = lib.mkDefault "${dotfilesNixDir}#${configurationName}";
    home.packages = [
      (configCommand.package config.programs.config)
    ];
  };
}
