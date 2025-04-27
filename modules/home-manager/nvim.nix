{
  config,
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
      unstable.nodejs
      tree-sitter
    ];
  };
  xdg.configFile."nvim".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/nvim";
  xdg.configFile."lazyvim".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/lazyvim";
}
