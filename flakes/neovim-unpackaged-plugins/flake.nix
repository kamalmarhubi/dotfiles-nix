{
  inputs = {
    acme-colors = {
      url = "github:plan9-for-vimspace/acme-colors";
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
      url = "github:kamalmarhubi/lazy-lsp.nvim/flake";
      flake = false;
    };
    "lush.nvim" = {
      url = "github:rktjmp/lush.nvim";
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
