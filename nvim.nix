{
  config,
  pkgs,
  ...
}: {
  home = {
    sessionVariables = {
      EDITOR = "nvim";
    };

    packages = with pkgs; [
      neovim-unwrapped
      nodejs
      tree-sitter
    ];
  };
  xdg.configFile."nvim".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.dataHome}/dotfiles-nix/files/nvim";
}
