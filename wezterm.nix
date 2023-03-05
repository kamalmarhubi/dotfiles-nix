{
  config,
  pkgs,
  ...
}: {
  home.packages = with pkgs; [
    wezterm
  ];

  xdg.configFile."wezterm".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.dataHome}/dotfiles-nix/files/wezterm";
}
