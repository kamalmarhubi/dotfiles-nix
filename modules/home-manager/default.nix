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
      ./llm.nix
      ./nvim.nix
      ./terminal.nix
      ./vcs.nix
    ]
    ++ extraModules;
  home.stateVersion = "22.11";
}
