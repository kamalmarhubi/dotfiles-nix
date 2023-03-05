{config, ...}: {
  xdg.configFile."kitty".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.dataHome}/dotfiles-nix/files/kitty";
  xdg.configFile."kitty.shell.conf".text = ''
    shell $HOME/.nix-profile/bin/fish --login --interactive
  '';
}
