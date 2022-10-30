{
  inputs = {
    #"leap.nvim" = {
    #  url = "github:ggandor/leap.nvim";
    #  flake = false;
    #};
  };

  outputs = inputs: {
    module = { lib, pkgs, ... }: let
      filteredInputs = builtins.removeAttrs inputs ["self"];
      buildNamedPlugin = name: input: pkgs.vimUtils.buildVimPlugin {
        inherit name;
	namePrefix = "";
	src = input;
      };
    in
    {
      programs.neovim.plugins = lib.mapAttrsToList buildNamedPlugin filteredInputs;
    };
  };
}
