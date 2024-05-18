{...}: {
  imports = [
    ../unfree.nix
    ./base.nix
    ./darwin.nix
    ./dotfiles-nix.nix
    ./emacs.nix
    ./fish.nix
    ./fonts.nix
    ./nvim.nix
    ./vcs.nix
    ./wezterm.nix
  ];
  home.stateVersion = "22.11";
}
