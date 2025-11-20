{
  config,
  inputs,
  system,
  pkgs,
  extraHomeModules ? [],
  ...
}: {
  imports = [
    ../unfree.nix
    # Use local modules until upstream nix-darwin modules are ready
    ./karabiner-driverkit-virtualhiddevice
    ./kanata
  ];
  system.stateVersion = 5;
  system.primaryUser = "kamal";

  # Use our new kanata + karabiner-driverkit setup
  # The kanata service automatically enables karabiner-driverkit-virtualhiddevice
  # and sets its package to kanata's darwinDriver for version coordination
  services.kanata = {
    enable = true;
    configFile = "${config.system.primaryUserHome}/.config/kanata/kanata.kbd";
    keepAlive = false;  # Easier for development - no auto-restart
  };

  # Set up touch id authentication for sudo.
  security.pam.services.sudo_local.touchIdAuth = true;
  # Require authentication on every sudo invocation.
  environment.etc."sudoers.d/10-timestamp_timeout".text = ''
    Defaults        timestamp_timeout=0
  '';
  nix.settings.experimental-features = "nix-command flakes";
  environment.shells = [pkgs.fish];
  programs.fish.enable = true;
  users.users.kamal.home = "/Users/kamal";
  users.users.kamal.name = "kamal";
  users.users.kamal.shell = pkgs.fish;
  home-manager.useGlobalPkgs = true;
  home-manager.useUserPackages = true;
  home-manager.users.kamal = import ../home-manager {extraModules = extraHomeModules;};
  home-manager.extraSpecialArgs = {
    inherit inputs system;
    dotFilesNixHomeManagerInstallationType = "nix-darwin";
  };
}
