{
  description = "Kamal pretends to use nix?";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    flake-utils.url = "github:numtide/flake-utils";
    mkalias = {
      url = "github:reckenrode/mkalias";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = {
    nixpkgs,
    home-manager,
    flake-utils,
    ...
  } @ inputs: let
    homeDirectoryFor = system: username:
      if builtins.match ".*darwin" system != null
      then "/Users/${username}"
      else "/home/${username}";
    homeFor = {
      username ? "kamal",
      system,
      homeDirectory ? homeDirectoryFor system username,
      extraModules ? [],
      ...
    }: let
      pkgs = nixpkgs.legacyPackages.${system};
    in
      home-manager.lib.homeManagerConfiguration {
        inherit pkgs;

        # Make inputs and system available to all modules.
        extraSpecialArgs = {inherit inputs system;};
        modules =
          [
            {
              nix.registry.nixpkgs.flake = nixpkgs;
              programs = {
                home-manager.enable = true;
              };

              home = {
                inherit username homeDirectory;
                stateVersion = "22.11";
              };
            }
            ./base.nix
            ./darwin.nix
            ./dotfiles-nix.nix
            ./fish.nix
            ./fonts.nix
            ./git.nix
            ./nvim.nix
            ./kitty.nix
            ./wezterm.nix
          ]
          ++ extraModules;
      };
  in
    {
      # For bootstrapping systems that aren't aleady in the homeConfigurations output.
      inherit homeFor;
      homeConfigurations = {
        "kamal@kx7" = homeFor {
          system = "x86_64-linux";
        };
        "kamal@mimolette" = homeFor {
          system = "aarch64-darwin";
        };
        "kamal@kamal-FL932PQ21V" = homeFor {
          system = "aarch64-darwin";
          extraModules = [./wave.nix];
        };
      };
    }
    // flake-utils.lib.eachDefaultSystem (system: {
      formatter = nixpkgs.legacyPackages.${system}.alejandra;
    });
}
