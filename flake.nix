{
  description = "Kamal pretends to use nix?";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    neovim = {
      # Switch this to use upstream neovim flake after 0.9.0 is released?
      # url = "github:neovim/neovim?dir=contrib";
      url = "github:nix-community/neovim-nightly-overlay";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    jj = {
      url = "github:martinvonz/jj";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    git-branchless = {
      url = "github:arxanas/git-branchless";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = {
    nixpkgs,
    home-manager,
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
            ./unfree.nix
            ./base.nix
            ./darwin.nix
            ./dotfiles-nix.nix
            ./emacs.nix
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
      inherit mkHomeConfig;
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
          extraModules = [./personal.nix];
        };
        "kamal@kamal-FL932PQ21V" = mkHomeConfig {
          system = "aarch64-darwin";
          extraModules = [./wave.nix];
        };
      };
    }
    // {
      formatter = eachSystem (system: nixpkgs.legacyPackages.${system}.alejandra);
    };
}
