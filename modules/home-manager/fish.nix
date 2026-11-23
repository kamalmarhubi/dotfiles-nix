{
  config,
  dotfilesNixDir,
  pkgs,
  ...
}: {
  programs.fish.enable = true;

  programs.starship = {
    enable = true;
    enableTransience = true;
  };

  xdg.configFile."starship.toml".source = config.lib.file.mkOutOfStoreSymlink "${dotfilesNixDir}/files/starship.toml";
  xdg.configFile."fish/functions".source = config.lib.file.mkOutOfStoreSymlink "${dotfilesNixDir}/files/fish/functions";
}
