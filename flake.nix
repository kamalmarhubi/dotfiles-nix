# TODOs
# - neovim
#   - cosmetics & aesthetics
#     - nonicons
#     - other tweaks:
#       - CursorLineNr, CursorLineSign, (CursorLineFold?)
#         - set these so that cursorline extends into gutter??
#       - Add toggle for `number signcolumn=number` <-> `nonumber signcolumn=yes:3`
#       - maybe something like: if LSP is attached or in gitrepo: set signcolumn=yes:3
#   - completion!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
#   - make git happen somehow
#     - neogit? lazygit? fugitive? git-messenger?
#   - add in plugins I know I like
#   - try new plugins
#     - configure dressing.nvim?
#     - nvim-treesitter-textsubjects
#     - some kind of swap-with-register thing
#     - editorconfig
#     - portal.nvim
#     - luasnip
#     - bqf
#     - hydra.nvim?
#     - treesitter things
#       - incremental_selection
#     - lsp niceties
#       - noice?
#       - trouble?
#       - lspsaga?
#       - one of the where-am-i things?
#       - improve diagnostics
#   - misc config
#     - laststatus=3?
#     - cmdheight=0?
#     - winbar?
#     - formatoptions: check `:h fo-table` for other useful stuff
#   - telescope
#     - figure out why <c-space> doesn't work to go from live_grep to fuzzy search (macos)
#   - print out new thing to put in packpath after activation? the way to do that would be via neovim not nix
#     - OR: add way to switch to current generation's packdir and reload everything
#       - `nvim -u NONE --headless '+lua print(vim.opt.packpath:get()[1])' +q` will print packdir from new neovim
#       - v:argv contains arguments of this running nvim: can be used to reset packpath
#   - lazy-lsp
#     - figure out a way to have it use the same nixpkgs as my home-manager config
# - misc:
#   - add a thing that checks /etc/pam.d/sudo for auth sufficient pam_tid.so and suggests adding it if absent
#   - similar check for yubikey?
{
  description = "Kamal pretends to use nix?";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    flake-utils.url = "github:numtide/flake-utils";
    neovim-unpackaged-plugins.url = "path:./flakes/neovim-unpackaged-plugins";
  };

  outputs = {
    nixpkgs,
    home-manager,
    flake-utils,
    neovim-unpackaged-plugins,
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
          neovim-unpackaged-plugins.module
          ({config, ...}: {
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
                  guess-indent-nvim
                  indent-blankline-nvim
                  # Go back to upstream when this PR is merged: https://github.com/dundalek/lazy-lsp.nvim/pull/3
                  # lazy-lsp-nvim
                  leap-nvim
                  legendary-nvim
                  # For some reason the one from nixpkgs does't include the plugin directory so it's a bit broken
                  # lush-nvim
                  neodev-nvim
                  nvim-cursorline
                  nvim-lspconfig
                  nvim-surround
                  playground  # nvim-tresitter/playground
                  plenary-nvim
                  telescope-nvim
                  telescope-fzf-native-nvim
                  (nvim-treesitter.withPlugins (_: pkgs.tree-sitter.allGrammars))
                  nvim-treesitter-textobjects
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
                (nerdfonts.override { fonts = [ "NerdFontsSymbolsOnly" ]; })
                (iosevka-bin.override {variant = "sgr-iosevka-fixed";})
                (iosevka-bin.override {variant = "sgr-iosevka-fixed-slab";})
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
            xdg.configFile."nvim".source = config.lib.file.mkOutOfStoreSymlink "${homeDirectory}/.local/share/dotfiles-nix/files/nvim";
            xdg.configFile."kitty".source = config.lib.file.mkOutOfStoreSymlink "${homeDirectory}/.local/share/dotfiles-nix/files/kitty";
          })
        ];
      };
  in
    {
      # For bootstrapping systems that aren't aleady in the homeConfigurations output.
      inherit homeFor;
      homeConfigurations = {
        "kamal@kx7" = homeFor {
          system = "x86_64-linux";
        };
        "kamal@kmbpwave.local" = homeFor {
          system = "aarch64-darwin";
        };
      };
    }
    // flake-utils.lib.eachDefaultSystem (system: {
      formatter = nixpkgs.legacyPackages.${system}.alejandra;
    });
}
