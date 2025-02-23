{
  config,
  pkgs,
  ...
}: {
  home.packages = with pkgs; [
    wezterm
  ];

  xdg.configFile."wezterm".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/wezterm";
  xdg.configFile."ghostty".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/ghostty";
}
