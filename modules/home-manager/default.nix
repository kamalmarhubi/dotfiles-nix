{extraModules ? [], ...}: {
  imports =
    [
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
    ]
    ++ extraModules;
  home.stateVersion = "22.11";
}
