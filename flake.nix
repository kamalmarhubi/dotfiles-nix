{
  description = "Kamal pretends to use nix?";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixpkgs-unstable";
    home-manager = {
      url = "github:kamalmarhubi/home-manager/flake-short-hostname";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = {
    nixpkgs,
    home-manager,
    flake-utils,
    ...
  }: let
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

        # Specify your home configuration modules here, for example,
        # the path to your home.nix.
        modules = [
          ./dotfiles-nix.nix
          ({config, ...}: {
            nix.registry.nixpkgs.flake = nixpkgs;
            fonts.fontconfig.enable = true;
            programs = {
              home-manager.enable = true;
              fish = {
                enable = true;
                shellInit = ''
                  source $HOME/.nix-profile/share/asdf-vm/asdf.fish
                '';
              };
            };

            home = {
              inherit username homeDirectory;
              stateVersion = "22.11";

              sessionVariables = {
                EDITOR = "nvim";
              };

              packages = with pkgs; [
                asdf-vm
                atool
                entr
                fd
                git
                git-lfs
                (nerdfonts.override {fonts = ["NerdFontsSymbolsOnly"];})
                (iosevka-bin.override {variant = "sgr-iosevka-fixed";})
                (iosevka-bin.override {variant = "sgr-iosevka-fixed-slab";})
                neovim-unwrapped
                poetry
                ripgrep
                pgformatter
                pstree
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
            xdg.configFile."nvim".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.dataHome}/dotfiles-nix/files/nvim";
            xdg.configFile."git/config".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.dataHome}/dotfiles-nix/files/git/config";
            xdg.configFile."git/config.dotfiles".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.dataHome}/dotfiles-nix/files/git/config.dotfiles";
            xdg.configFile."kitty".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.dataHome}/dotfiles-nix/files/kitty";
            xdg.configFile."kitty.shell.conf".text = ''
              shell ${homeDirectory}/.nix-profile/bin/fish --login --interactive
            '';
          })
        ] ++ extraModules;
      };
  in
    {
      # For bootstrapping systems that aren't aleady in the homeConfigurations output.
      inherit homeFor;
      homeConfigurations = {
        "kamal@kx7" = homeFor {
          system = "x86_64-linux";
        };
        "kamal@kmbpwave" = homeFor {
          system = "aarch64-darwin";
        };
        "kamal@kamal-FL932PQ21V" = homeFor {
          system = "aarch64-darwin";
          extraModules = [({config, ...}: {
            xdg.configFile."git/config.local".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.dataHome}/dotfiles-nix/files/git/config.wave";
          })];
        };
      };
    }
    // flake-utils.lib.eachDefaultSystem (system: {
      formatter = nixpkgs.legacyPackages.${system}.alejandra;
    });
}
