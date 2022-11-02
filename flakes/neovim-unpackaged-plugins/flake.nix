{
  inputs = {
    acme-colors = {
      url = "github:plan9-for-vimspace/acme-colors";
      flake = false;
    };
    "import.nvim" = {
      url = "github:miversen33/import.nvim";
      flake = false;
    };
    "monotone.nvim" = {
      url = "github:Lokaltog/monotone.nvim";
      flake = false;
    };
    vim-bw = {
      url = "git+https://git.goral.net.pl/mgoral/vim-bw.git";
      flake = false;
    };
    "lazy-lsp.nvim" = {
      url = "github:kamalmarhubi/lazy-lsp.nvim/empty-config";
      flake = false;
    };
  };

  outputs = inputs: {
    module = {
      lib,
      pkgs,
      ...
    }: let
      filteredInputs = builtins.removeAttrs inputs ["self"];
      buildNamedPlugin = name: input:
        pkgs.vimUtils.buildVimPlugin {
          inherit name;
          namePrefix = "";
          src = input;
        };
    in {
      programs.neovim.plugins = lib.mapAttrsToList buildNamedPlugin filteredInputs;
    };
  };
}
