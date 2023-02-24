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
    asdf-vm
    git-lfs
    k9s
    (wrapHelm kubernetes-helm {plugins = [kubernetes-helmPlugins.helm-diff];})
    pgcli
    pgformatter
  ];

  xdg.configFile."git/config.local".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.dataHome}/dotfiles-nix/files/git/config.wave";
}
