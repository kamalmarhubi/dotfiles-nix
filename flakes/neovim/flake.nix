{
  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";

    leap-nvim = {
      url = "github:ggandor/leap.nvim";
      flake = false;
    };
  };

  outputs = {
    nixpkgs,
    flake-utils,
    leap-nvim,
    ...
  }:
    flake-utils.lib.eachDefaultSystem (system: let
      pkgs = nixpkgs.legacyPackages.${system};
    in {
      packages = {
        leap = pkgs.vimUtils.buildVimPlugin {
          name = "leap.nvim";
          src = leap-nvim;
        };
      };
    });
}
