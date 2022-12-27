{
  description = "Kamal pretends to use nix?";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    home-manager = {
      url = "github:kamalmarhubi/home-manager/flake-short-hostname";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    flake-utils.url = "github:numtide/flake-utils";

    # This is unfortunate: I'd prefer to keep this noise out of the main flake.
    # But subflakes are super broken, possibly eventually to be fixed in
    #   https://github.com/NixOS/nix/pull/6530 maybe?
    # Anyway for now, here's the unpackaged neovim plugins. Maybe motivation to get them added to nixpkgs.
    acme-colors = {
      url = "github:plan9-for-vimspace/acme-colors";
      flake = false;
    };
    "monotone.nvim" = {
      url = "github:Lokaltog/monotone.nvim";
      flake = false;
    };
    vim-bw = {
      url = "git+https://git.goral.net.pl/mgoral/vim-bw.git";
      flake = false;
    };
    "lush.nvim" = {
      url = "github:rktjmp/lush.nvim";
      flake = false;
    };
    "zenbones.nvim" = {
      url = "github:mcchrish/zenbones.nvim";
      flake = false;
    };
    "possession.nvim" = {
      url = "github:jedrzejboczar/possession.nvim";
      flake = false;
    };
  };

  outputs = inputs @ {
    nixpkgs,
    home-manager,
    flake-utils,
    ...
  }: let
    vimPluginInputs = builtins.removeAttrs inputs ["self" "nixpkgs" "home-manager" "flake-utils"];
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
          (
            {
              lib,
              pkgs,
              inputs,
              ...
            }: let
              buildNamedPlugin = name: input:
                pkgs.vimUtils.buildVimPlugin {
                  inherit name;
                  namePrefix = "";
                  src = input;
                };
            in {
              programs.neovim.plugins = lib.mapAttrsToList buildNamedPlugin vimPluginInputs;
            }
          )

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
              neovim = {
                enable = true;
                plugins = with pkgs.vimPlugins; [
                  comment-nvim
                  dressing-nvim
                  gitlinker-nvim
                  guess-indent-nvim
                  indent-blankline-nvim
                  lazy-lsp-nvim
                  leap-nvim
                  legendary-nvim
                  # For some reason the one from nixpkgs does't include the plugin directory so it's a bit broken
                  # lush-nvim
                  neoconf-nvim
                  neodev-nvim
                  nvim-lspconfig
                  nvim-surround
                  playground # nvim-tresitter/playground
                  plenary-nvim
                  telescope-nvim
                  telescope-fzf-native-nvim
                  (nvim-treesitter.withPlugins (_: pkgs.tree-sitter.allGrammars))
                  nvim-treesitter-textobjects
                  toggleterm-nvim
                  nvim-web-devicons
                  vim-unimpaired
                  which-key-nvim
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
                asdf-vm
                atool
                entr
                fd
                git
                git-lfs
                (nerdfonts.override {fonts = ["NerdFontsSymbolsOnly"];})
                (iosevka-bin.override {variant = "sgr-iosevka-fixed";})
                (iosevka-bin.override {variant = "sgr-iosevka-fixed-slab";})
                poetry
                ripgrep
                pgformatter
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
