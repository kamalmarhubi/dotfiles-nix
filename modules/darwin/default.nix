{
  inputs,
  system,
  pkgs,
  extraHomeModules ? [],
  ...
}: {
  imports = [
    ./sudo.nix
    ../unfree.nix
  ];
  system.stateVersion = 5;
  nixpkgs.config.allowUnfree = true;
  services.nix-daemon.enable = true;
  # services.karabiner-elements.enable = true;
  # nix.configureBuildUsers = true;
  nix.settings.experimental-features = "nix-command flakes";
  environment.shells = [pkgs.fish];
  programs.fish.enable = true;
  users.users.kamal.home = "/Users/kamal";
  users.users.kamal.name = "kamal";
  users.users.kamal.shell = pkgs.fish;
  system.activationScripts.postActivation.text = ''
    dscl . -create '/Users/kamal' UserShell '/run/current-system/sw${pkgs.fish.shellPath}'
  '';
  home-manager.useGlobalPkgs = true;
  home-manager.useUserPackages = true;
  home-manager.users.kamal = import ../home-manager {extraModules = extraHomeModules;};
  home-manager.extraSpecialArgs = {
    inherit inputs system;
    dotFilesNixHomeManagerInstallationType = "nix-darwin";
  };
}
