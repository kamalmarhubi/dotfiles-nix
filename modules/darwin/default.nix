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

  # Use Lix (modern Nix implementation) - advanced configuration
  nixpkgs.overlays = [ (final: prev: {
    inherit (final.lixPackageSets.stable)
      nixpkgs-review
      nix-direnv
      nix-eval-jobs
      nix-fast-build
      colmena;
  }) ];

  nix.package = pkgs.lixPackageSets.stable.lix;

  # Use our new kanata + karabiner-driverkit setup
  services.kanata = {
    enable = true;
    # TODO^2: Uncomment below two lines after getting nix to 2.18 or higher.
    # TODO: Remove after v1.10.0 is released - https://github.com/jtroo/kanata/releases
    # package = pkgs.mine.kanata;
    configFile = "${config.system.primaryUserHome}/.config/kanata/kanata.kbd";
    keepAlive = false;  # Easier for development - no auto-restart
  };

  services.karabiner-driverkit-virtualhiddevice = {
    enable = true;
    package = pkgs.master.karabiner-dk;
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
