{
  config,
  configurationName,
  lib,
  pkgs,
  ...
}: let
  configCommand = import ../config-command.nix {
    inherit lib pkgs;
    manager = "nix-darwin";
    managerCmd = "${config.system.build.darwin-rebuild}/bin/darwin-rebuild";
    dotfilesNixDir = "${config.system.primaryUserHome}/repos/github.com/kamalmarhubi/dotfiles-nix";
  };
in {
  options.programs.config = configCommand.options;

  config = {
    programs.config.flake = lib.mkDefault "${config.system.primaryUserHome}/repos/github.com/kamalmarhubi/dotfiles-nix#${configurationName}";
    environment.systemPackages = [
      (configCommand.package config.programs.config)
    ];
  };
}
