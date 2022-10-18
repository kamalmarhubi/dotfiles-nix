{
  description = "Kamal pretends to use nix?";

  inputs = {
    # Specify the source of Home Manager and Nixpkgs.
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    neovim-nightly-overlay.url = "github:nix-community/neovim-nightly-overlay";
  };

  outputs = { nixpkgs, home-manager, ... }@inputs:
    let
      overlays = [
        inputs.neovim-nightly-overlay.overlay
      ];
    in {
    homeConfigurations = {
      "kamal@kx7" = let
        system = "x86_64-linux";
        pkgs = nixpkgs.legacyPackages.${system};
      in
      home-manager.lib.homeManagerConfiguration {
        inherit pkgs;

        # Specify your home configuration modules here, for example,
        # the path to your home.nix.
        modules = [({ pkgs, ... }: {
          nixpkgs.overlays = overlays;
          programs = {
            home-manager.enable = true;
            neovim = {
              enable = true;
              package = pkgs.neovim-nightly;
              plugins = with pkgs.vimPlugins; [
              ];
            };
          };

          home = {
            username = "kamal";
            homeDirectory = "/home/kamal";
            stateVersion = "22.11";

            packages = with pkgs; [
            ];

            sessionVariables = {
              EDITOR = "nvim";
            };
          };
        })];
      };
    };
  };
}
