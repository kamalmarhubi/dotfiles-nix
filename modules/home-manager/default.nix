{extraModules ? [], ...}: {
  imports =
    [
      ../unfree.nix
      ./base.nix
      ./dotfiles-nix.nix
      ./emacs.nix
      ./fish.nix
      ./fonts.nix
      ./llm.nix
      ./nvim.nix
      ./rust.nix
      ./terminal.nix
      ./vcs.nix
    ]
    ++ extraModules;
  home.stateVersion = "25.11";
}
