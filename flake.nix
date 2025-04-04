{
  description = "Kamal pretends to use nix?";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-24.11-darwin";
    unstable.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    nur = {
      url = "github:nix-community/NUR";
      inputs.nixpkgs.follows = "unstable";
    };
    home-manager = {
      url = "github:nix-community/home-manager/release-24.11";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    nix-darwin = {
      url = "github:LnL7/nix-darwin/nix-darwin-24.11";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # Hack because I just need neovim to work.
    nixpkgs-for-neovim.url = "github:nixos/nixpkgs/nixpkgs-24.05-darwin";
    neovim = {
      # Switch this to use upstream neovim flake after 0.9.0 is released?
      # url = "github:neovim/neovim?dir=contrib";
      url = "github:nix-community/neovim-nightly-overlay/ef968b8411938ee11ed8a3a5c5b46cba4bdbe142";
      inputs.nixpkgs.follows = "nixpkgs-for-neovim";
    };
  };

  outputs = {
    nixpkgs,
    home-manager,
    nix-darwin,
    nur,
    ...
  } @ inputs: let
    systems = [
      "aarch64-darwin"
      "aarch64-linux"
      "x86_64-darwin"
      "x86_64-linux"
    ];
    eachSystem = nixpkgs.lib.genAttrs systems;
    isDarwin = system: builtins.elem system inputs.nixpkgs.lib.platforms.darwin;
    homePrefix = system:
      if isDarwin system
      then "/Users"
      else "/home";

    registryConfig = {
      nix.registry.nixpkgs.flake = inputs.nixpkgs;
      nix.registry.unstable.flake = inputs.unstable;
    };
    mkDarwinConfig = {
      system ? "aarch64-darwin",
      extraModules ? [],
      extraHomeModules ? [],
    }:
      nix-darwin.lib.darwinSystem {
        inherit system;
        modules =
          [
            nur.modules.darwin.default
            home-manager.darwinModules.home-manager
            registryConfig
            ./modules/darwin
          ]
          ++ extraModules;
        specialArgs = {inherit inputs system extraHomeModules;};
      };

    mkHomeConfig = {
      username ? "kamal",
      system,
      homeDirectory ? "${homePrefix system}/${username}",
      extraModules ? [],
      ...
    }: let
      pkgs = nixpkgs.legacyPackages.${system};
    in
      home-manager.lib.homeManagerConfiguration {
        inherit pkgs;

        # Make inputs and system available to all modules.
        extraSpecialArgs = {
          inherit inputs system;
          dotFilesNixHomeManagerInstallationType = "standalone";
        };
        modules =
          [
            nur.modules.homeManager.default
            ./modules/home-manager
            registryConfig
            {
              programs.home-manager.enable = true;
              home = {
                inherit username homeDirectory;
              };
            }
          ]
          ++ extraModules;
      };
  in
    {
      darwinConfigurations = {
        "bootstrap" = mkDarwinConfig {
          system = builtins.currentSystem;
        };

        "kamal-FL932PQ21V" = mkDarwinConfig {
          system = "aarch64-darwin";
          extraHomeModules = [./modules/home-manager/wave.nix];
        };
        # Huh? Logging in to the kamal-personal account seems to change the hostname?
        "kamal-personal-FL932PQ21V" = mkDarwinConfig {
          system = "aarch64-darwin";
          extraHomeModules = [./modules/home-manager/wave.nix];
        };
        "mimolette" = mkDarwinConfig {
          system = "aarch64-darwin";
        };
      };
      homeConfigurations = {
        # For bootstrapping systems that aren't aleady in the homeConfigurations output.
        # NB This reads env vars and so requires --impure.
        "bootstrap" = mkHomeConfig {
          system = builtins.currentSystem;
          username = builtins.getEnv "USER";
          homeDirectory = builtins.getEnv "HOME";
        };

        "kamal@kx7" = mkHomeConfig {
          system = "x86_64-linux";
        };
        "kamal@mimolette" = mkHomeConfig {
          system = "aarch64-darwin";
          extraModules = [./modules/home-manager/personal.nix];
        };
        "kamal@kamal-FL932PQ21V" = mkHomeConfig {
          system = "aarch64-darwin";
          extraModules = [./modules/home-manager/wave.nix];
        };
        # Huh? Logging in to the kamal-personal account seems to change the hostname?
        "kamal@kamal-personal-FL932PQ21V" = mkHomeConfig {
          system = "aarch64-darwin";
          extraModules = [./modules/home-manager/wave.nix];
        };
      };
    }
    // {
      formatter = eachSystem (system: nixpkgs.legacyPackages.${system}.alejandra);
    };
}
