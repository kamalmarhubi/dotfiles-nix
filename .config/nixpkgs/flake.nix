{
  description = "Kamal pretends to use nix?";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { nixpkgs, home-manager, ... }: {
    homeConfigurations = {
      "kamal@kx7" = let
        system = "x86_64-linux";
        pkgs = nixpkgs.legacyPackages.${system};
      in
      home-manager.lib.homeManagerConfiguration {
        inherit pkgs;

        # Specify your home configuration modules here, for example,
        # the path to your home.nix.
        modules = [{
          programs = {
            home-manager.enable = true;
            fish.enable = true;
            neovim.enable = true;
          };

          home = {
            username = "kamal";
            homeDirectory = "/home/kamal";
            stateVersion = "22.11";

            sessionVariables = {
              EDITOR = "nvim";
            };
          };
        }];
      };
    };
  };
}
