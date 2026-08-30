{
  config,
  dotfilesNixDir,
  pkgs,
  inputs,
  system,
  ...
}: {
  home = {
    sessionVariables = {
      EDITOR = "nvim";
    };

    packages = with pkgs; [
      inputs.neovim.packages.${system}.default
      nodejs
      tree-sitter
    ];
  };
  xdg.configFile."nvim".source = config.lib.file.mkOutOfStoreSymlink "${dotfilesNixDir}/files/nvim";
  xdg.configFile."lazyvim".source = config.lib.file.mkOutOfStoreSymlink "${dotfilesNixDir}/files/lazyvim";
}
