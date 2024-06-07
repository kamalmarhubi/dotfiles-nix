# Things one could do

Look at: https://trello.com/b/lobyPOvA/dev-tools

## general nix/home-manager

- move work specific stuff out of main config so I can commit it

## general nix system

Idea: add nix-darwin to manage
- mac app store applications
- homebrew stuff as needed
- variety of defaults
- launchd agent or daemon to ensure that pam_tid.so is in /etc/pam.d/sudo
  Note: this would be made unnecessary by https://github.com/LnL7/nix-darwin/pull/787

Things to read about managing home-manager + nix-darwin:
- https://github.com/kclejeune/system
- https://xyno.space/post/nix-darwin-introduction

### desired properties

I like having the bootstrap command; can that be made to work with a nix-darwin managed thingy?

#### 2024-02-04
looks like answer to this is "yes" but with a bit of care. some sources I've looked at:
- https://www.reddit.com/r/NixOS/comments/jznwne/effectively_combining_homemanager_and_nixdarwin/
  - https://github.com/kclejeune/system


Some frustrations: it would be ideal to set [useUserPackages] to true, however it breaks my path.

[useUserPackages]: https://nix-community.github.io/home-manager/nix-darwin-options.xhtml#nix-darwin-opt-home-manager.useUserPackages

Idea to fix this: create a launchd thing that calls launchctl setenv to ensure path is at the front
needs: path_helper
also needed to make emacs work?

 I did this?

## neovim

## Learning from actually doing any work
- ideally save session periodically; lost some stuff because of nvim crash
  - timer?
  - BufEnter?
- completion, even if manually triggered
  - maybe use omnifunc for now?
- jumping around differeng files... <leader>fb is slightly annoying
  - figure out portal and/or grapple?
- juggling multiple open projects... session-per-tab? terminal tabs?
  - realistically, it's still one purpose
    - looking at graphql source while working on our graphql stuff
    - or same for otel
    - so maybe just open those repos up in tabs in the same session?

### sessions

- switch to resession for $reasons?

### nix bs

- lazy-lsp
  - figure out a way to have it use the same nixpkgs as my home-manager config

#### config editability

TODO: figure out which parts of this might still be needed after dumping the
home-manager module.

something kind of complicated but maybe doable?

on home-manager switch invocation:
- save "empty" state
  - launch nvim -u NONE
  - things to save:
    - options
      - regular
      - local
      - global
    - variables
    - lua state
      - possibly easy: there's not much in _G on startup. could clear out everything from `package.loaded` that isn't in `package.preload`?
    - autocommands so we can clear ones added by config & plugins before re-init
      - uh but need to rerun autocmds for buffers, eg TermOpen?
        - not so bad: https://vimdoc.sourceforge.net/htmldoc/autocmd.html#:doautoall
  - save:
    - `:let` output
    - `:set` output
    - `:lua =_G`

  - OR
    - restore to default with `:set all&`


### code editor experience

### lsp
- turn off the stupid spelling language server??
- noice?
- trouble?
- lsp_lines
- lspsaga?
- one of the where-am-i things?
- improve diagnostics

### misc
- completion!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
  - coq vs cmp?
  - snippets
    - luasnip (https://github.com/L3MON4D3/LuaSnip)
      - collections? https://github.com/L3MON4D3/LuaSnip#add-snippets
- nvim-treesitter-textsubjects
- some kind of swap-with-register thing
- editorconfig
- treesitter things
  - incremental_selection

### terminal
- make there be a way to escape insert mode in toggleterm
- add dedicated toggleterms for k9s htop some other stuff
- maybe a way to have non-floating terms / multiple terms?

### git
- figure out what plugins
  - neogit? lazygit? fugitive? git-messenger?
  - gitsigns.nvim
  - git-messenger.vim


### cosmetics

- cosmetics & aesthetics
  - nonicons
  - other tweaks:
    - CursorLineNr, CursorLineSign, (CursorLineFold?)
      - set these so that cursorline extends into gutter??
    - Add toggle for `number signcolumn=number` <-> `nonumber signcolumn=yes:3`
    - maybe something like: if LSP is attached or in gitrepo: set signcolumn=yes:3

### misc
- add in plugins I know I like
  - Plugins from my old setup:
    - vim-abolish
    - vim-doge
    - vim-floaterm
    - vimwiki et al
      - taskwiki
      - vim-taskwarrior
      - vimwiki

- try new plugins
  - dap
    - https://github.com/mfussenegger/nvim-dap
  - bqn
    - https://sr.ht/~detegr/nvim-bqn/
  - configure dressing.nvim?
  - portal.nvim
  - bqf
  - hydra.nvim?
  - image display?
    - https://github.com/edluffy/hologram.nvim
- misc config
  - laststatus=3?
  - cmdheight=0?
  - winbar?
  - formatoptions: check `:h fo-table` for other useful stuff
- telescope
  - figure out why <c-space> doesn't work to go from live_grep to fuzzy search (macos)


## system
- add a thing that checks /etc/pam.d/sudo for auth sufficient pam_tid.so and suggests adding it if absent
- similar check for yubikey?

## firefox
### userChrome.css

```css
#main-window[titlepreface*="‌"] #sidebar-header {
  display: none;
}

#main-window[titlepreface*="‌"] #TabsToolbar {
  display: none;
}
```
