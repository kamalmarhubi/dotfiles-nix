{
  config,
  dotfilesNixDir,
  pkgs,
  ...
}: {
  home.packages = with pkgs; [
    wezterm
  ];

  xdg.configFile."wezterm".source = config.lib.file.mkOutOfStoreSymlink "${dotfilesNixDir}/files/wezterm";
  xdg.configFile."ghostty".source = config.lib.file.mkOutOfStoreSymlink "${dotfilesNixDir}/files/ghostty";
}
