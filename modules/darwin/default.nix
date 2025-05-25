{
  inputs,
  system,
  pkgs,
  extraHomeModules ? [],
  ...
}: {
  imports = [
    ../unfree.nix
  ];
  system.stateVersion = 5;
  # services.karabiner-elements.enable = true;

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
