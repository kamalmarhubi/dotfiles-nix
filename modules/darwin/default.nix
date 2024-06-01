{
  inputs,
  system,
  pkgs,
  ...
}: {
  imports = [
    ./sudo.nix
  ];
  nixpkgs.config.allowUnfree = true;
  services.nix-daemon.enable = true;
  services.karabiner-elements.enable = true;
  nix.settings.experimental-features = "nix-command flakes";
  environment.shells = [ pkgs.fish ];
  programs.fish.enable = true;
  users.users.kamal.home = "/Users/kamal";
  users.users.kamal.name = "kamal";
  users.users.kamal.shell = pkgs.fish;
  system.activationScripts.postActivation.text = ''
    dscl . -create '/Users/kamal' UserShell '/run/current-system/sw${pkgs.fish.shellPath}'
  '';
  home-manager.useGlobalPkgs = true;
  home-manager.useUserPackages = true;
  home-manager.users.kamal = import ../home-manager;
  home-manager.extraSpecialArgs = {
    inherit inputs system;
    dotFilesNixHomeManagerInstallationType = "nix-darwin";
  };
}
