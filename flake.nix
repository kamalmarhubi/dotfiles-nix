{
  description = "Kamal pretends to use nix?";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
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
    jj = {
      url = "github:martinvonz/jj";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    git-branchless = {
      url = "github:arxanas/git-branchless";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    # TODO: Replace with jvsn/dig-pretty afther this PR merges:
    #   https://github.com/jvns/dig-pretty/pull/2
    dig-pretty = {
      url = "github:kamalmarhubi/dig-pretty/push-kwolwwmonnql";
      flake = false;
    };
  };

  outputs = {
    nixpkgs,
    home-manager,
    ...
  } @ inputs: let
    eachSystem = nixpkgs.lib.genAttrs [
      "aarch64-darwin"
      "aarch64-linux"
      "x86_64-darwin"
      "x86_64-linux"
    ];
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
              nixpkgs.config.allowUnfreePredicate = _: true;
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
    // {
      formatter = eachSystem (system: nixpkgs.legacyPackages.${system}.alejandra);
    };
}
