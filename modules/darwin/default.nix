{
  inputs,
  system,
  ...
}: {
  imports = [../home-manager/unfree.nix];
  nixpkgs.config.allowUnfree = true;
  services.nix-daemon.enable = true;
  services.karabiner-elements.enable = true;
  nix.settings.experimental-features = "nix-command flakes";
  programs.fish.enable = true;
  users.users.kamal.home = "/Users/kamal";
  users.users.kamal.name = "kamal";
  security.pam.enableSudoTouchIdAuth = true;
  environment.etc."sudoers.d/10-timestamp_timeout".text = ''
    Defaults        timestamp_timeout=0
  '';
  home-manager.useGlobalPkgs = true;
  # home-manager.useUserPackages = true;
  home-manager.users.kamal = import ../home-manager;
  home-manager.extraSpecialArgs = {inherit inputs system;};
}
