# Things one could do

## Learning from actually doing any work
- ideally save session periodically; lost some stuff because of nvim crash
  - timer?
  - BufEnter?
- completion, even if manually triggered
  - maybe use omnifunc for now?
- jumping around differeng files... <leader>fb is slightly annoying
  - figure out portal and/or grapple?
- juggling multiple open projects... session-per-tab? terminal tabs?

## neovim

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
#main-window[titlepreface*="???"] #sidebar-header {
  display: none;
}

#main-window[titlepreface*="???"] #TabsToolbar {
  display: none;
}
```
