{
  config,
  pkgs,
  lib,
  ...
}: {
  programs.fish.shellInit = ''
    source $HOME/.nix-profile/share/asdf-vm/asdf.fish
  '';

  home.packages = with pkgs; [
    argocd
    asdf-vm
    cmake
    colordiff
    git-lfs
    k9s
    (wrapHelm kubernetes-helm {plugins = [kubernetes-helmPlugins.helm-diff];})
    ninja
    pgcli
    pgformatter
  ];

  xdg.configFile."git/config.local".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.dataHome}/dotfiles-nix/files/git/config.wave";
}
