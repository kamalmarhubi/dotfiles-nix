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
    colima
    colordiff
    git-lfs
    go
    k9s
    (wrapHelm kubernetes-helm {plugins = [kubernetes-helmPlugins.helm-diff];})
    kustomize
    ninja
    pgcli
    pgformatter
  ];

  xdg.configFile."git/config.local".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/git/config.wave";
}
