{
  inputs = {
    "leap.nvim" = {
      url = "github:ggandor/leap.nvim";
      flake = false;
    };
  };

  outputs = inputs: {
    module = { config, lib, pkgs, ... }: let
      homeDirectory = config.home.homeDirectory;
      buildNamedPlugin = name: input: pkgs.vimUtils.buildVimPlugin {
        inherit name;
	namePrefix = "";
	src = input;
      };
    in
    {
      programs.neovim = {
        enable = true;
        plugins = lib.mapAttrsToList buildNamedPlugin inputs;
      };
      xdg.configFile."nvim".source = config.lib.file.mkOutOfStoreSymlink "${homeDirectory}/.local/share/dotfiles-nix/files/nvim";
    };
  };
}
