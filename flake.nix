# Remaining:
# - start using it at work
{
  description = "Kamal pretends to use nix?";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    flake-utils.url = "github:numtide/flake-utils";

    # Neovim plugins
    leap-nvim = {
      url = "github:ggandor/leap.nvim";
      flake = false;
    };
  };

  outputs = {
    nixpkgs,
    home-manager,
    flake-utils,
    leap-nvim,
    ...
  }: let
    homeDirectoryFor = system: username:
      if builtins.match ".*darwin" system != null
      then "/Users/${username}"
      else "/home/${username}";
    mkConfig = {
      username ? "kamal",
      system,
      ...
    }: let
      homeDirectory = homeDirectoryFor system username;
      pkgs = nixpkgs.legacyPackages.${system};
      leap = pkgs.vimUtils.buildVimPlugin {
        name = "leap.nvim";
        src = leap-nvim;
      };
    in
      home-manager.lib.homeManagerConfiguration {
        inherit pkgs;

        # Specify your home configuration modules here, for example,
        # the path to your home.nix.
        modules = [
          ./dotfiles-nix.nix
          ({config, ...}: {
            programs = {
              home-manager.enable = true;
              fish.enable = true;
              neovim = {
                enable = true;
                plugins = with pkgs.vimPlugins; [
                  leap
                ];
              };
            };

            home = {
              inherit username homeDirectory;
              stateVersion = "22.11";

              sessionVariables = {
                EDITOR = "nvim";
              };

              packages = with pkgs; [
                atool
                entr
                fd
                ripgrep
                pv
                tree
                wget
              ];
            };
            # For whatever reason, the installer didn't put this somewhere that
            # fish would see. Since the nix-daemon.fish file guards against
            # double-sourcing, there's no harm including this in all systems.
            xdg.configFile."fish/conf.d/nix.fish".text = ''
              # Nix
              if test -e '/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.fish'
                . '/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.fish'
              end
              # End Nix
            '';
            xdg.configFile."nvim".source = config.lib.file.mkOutOfStoreSymlink "${homeDirectory}/.local/share/dotfiles-nix/files/nvim";
          })
        ];
      };
  in
    {
      homeConfigurations = rec {
        genericLinux = mkConfig {
          system = "x86_64-linux";
        };
        genericMacos = mkConfig {
          system = "aarch64-darwin";
        };
        "kamal@kx7" = genericLinux;
        "kamal@kmbpwave.local" = genericMacos;
      };
    }
    // flake-utils.lib.eachDefaultSystem (system: {
      formatter = nixpkgs.legacyPackages.${system}.alejandra;
    });
}
