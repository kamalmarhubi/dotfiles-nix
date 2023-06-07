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
    neovim = {
      # Switch this to use upstream neovim flake after 0.9.0 is released?
      # url = "github:neovim/neovim?dir=contrib";
      url = "github:nix-community/neovim-nightly-overlay";
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
              # Workaround for https://github.com/nix-community/home-manager/issues/2942
              nixpkgs.config.allowUnfreePredicate = (_: true);
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
            ./nvim.nix
            ./vcs.nix
            ./wezterm.nix
          ]
          ++ extraModules;
      };
  in
    {
      # For bootstrapping systems that aren't aleady in the homeConfigurations output.
      inherit homeFor;
      homeConfigurations = {
        # NB This reads env vars and so requires --impure.
        "bootstrap" = homeFor {
          system = builtins.currentSystem;
          username = builtins.getEnv "USER";
          homeDirectory = builtins.getEnv "HOME";
        };

        "kamal@kx7" = homeFor {
          system = "x86_64-linux";
        };
        "kamal@mimolette" = homeFor {
          system = "aarch64-darwin";
          extraModules = [./personal.nix];
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
