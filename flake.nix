{
  description = "Kamal pretends to use nix?";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-25.05-darwin";

    # Temporarily use unmerged PR upgrading claude-code past 1.0.111 for token
    # count output while processing.
    unstable.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    master.url = "github:nixos/nixpkgs/master";

    # Catch the bump to 1.10 which uses karabiner-dk 6:
    #   https://github.com/NixOS/nixpkgs/pull/461152
    # TODO(25.11): At some point this will be available in unstable.
    nixpkgs-kanata-pr.url = "github:nixos/nixpkgs/pull/461152/head";
    nur = {
      url = "github:nix-community/NUR";
      inputs.nixpkgs.follows = "unstable";
    };
    home-manager = {
      # TODO(25.11): Switch back from master; change nixpkgs.follows back to nixpkgs.
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "unstable";
    };
    nix-darwin = {
      url = "github:nix-darwin/nix-darwin/nix-darwin-25.05";
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
    unstable,
    master,
    home-manager,
    nix-darwin,
    nur,
    ...
  } @ inputs: let
    mkOverlayModule = overlay: {
      nixpkgs.overlays = [overlay];
    };
    unstableOverlayModule = mkOverlayModule (
      final: prev: {
        unstable = import unstable {
          system = prev.system;
          config = prev.config;
        };
      }
    );
    masterOverlayModule = mkOverlayModule (
      final: prev: {
        master = import master {
          system = prev.system;
          config = prev.config;
        };
      }
    );
    # Helper to make it easy to replace specific packges from a different
    # nixpkgs; useful for catching unmerged PRs or other branches. The
    # packageInputMap argument is an attrSet like
    #     with inputs; { some-package-name = some-input-name; }
    # which would result in using some-package-name from the referenced input.
    mkPackageReplacementOverlayModule = packageInputMap:
      mkOverlayModule (
        final: prev:
          builtins.foldl'
            (acc: packageName:
              let
                nixpkgsInput = packageInputMap.${packageName};
                pkgs = import nixpkgsInput {
                  system = prev.system;
                  config = prev.config;
                };
              in
                acc // { ${packageName} = pkgs.${packageName}; }
            )
            {}
            (builtins.attrNames packageInputMap)
      );
    # A module to use lix instead of CppNix. Adapated from
    #   https://lix.systems/add-to-config/#advanced-change
    # This should not be included in standalone home-manager configs.
    lixModule = {pkgs, ...}: {
      nixpkgs.overlays = [
        (final: prev: {
          inherit (final.lixPackageSets.stable)
            nixpkgs-review
            nix-direnv
            nix-eval-jobs
            nix-fast-build
            colmena;
        })
      ];
      nix.package = pkgs.lixPackageSets.stable.lix;
    };
    myPackagesOverlayModule = import ./pkgs { lib = nixpkgs.lib; };

    # Common overlays used by both darwin and standalone home-manager configs
    commonOverlayModules = [
      unstableOverlayModule
      masterOverlayModule
      myPackagesOverlayModule
      (mkPackageReplacementOverlayModule (with inputs; {
        kanata = nixpkgs-kanata-pr;
      }))
    ];

    # Common modules used by both darwin and standalone home-manager configs
    commonModules = commonOverlayModules ++ [
      registryConfig
    ];

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
      nix.registry.master.flake = inputs.master;
    };
    mkDarwinConfig = {
      system ? "aarch64-darwin",
      extraModules ? [],
      extraHomeModules ? [],
    }:
      nix-darwin.lib.darwinSystem {
        inherit system;
        modules =
          commonModules
          ++ [
            lixModule
            nur.modules.darwin.default
            home-manager.darwinModules.home-manager
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
          commonModules
          ++ [
            nur.modules.homeManager.default
            ./modules/home-manager
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
          # Error message is confusing since I don't think I did any of these:
          #   Possible causes include setting up a new Nix installation with an
          #   existing nix-darwin configuration, setting up a new nix-darwin
          #   installation with an existing Nix installation, or manually increasing
          #   your `system.stateVersion` setting.
          extraModules = [
            {ids.gids.nixbld = 30000;} # New default is 350.
          ];
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
