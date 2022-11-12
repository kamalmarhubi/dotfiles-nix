vim.opt.shortmess:append("I")
vim.opt.termguicolors = true
vim.opt.background = "light"
vim.cmd.colorscheme('neobones')

vim.g.mapleader = ' '
vim.o.timeoutlen = 500

vim.opt.formatoptions:append("r")  -- respect comment on newline in insert
vim.opt.formatoptions:append("o")  -- respect comment on o in normal

-- Super handy command mode abbreviation for the directory of the current file.
-- From
--   http://vim.wikia.com/wiki/Easy_edit_of_files_in_the_same_directory#Using_a_command_line_abbreviation
vim.cmd[[cabbr <expr> %% expand('%:p:h')]]

require('leap').add_default_mappings()
require('Comment').setup()
require('guess-indent').setup()
require('nvim-surround').setup()
-- This is required before lspconfig is set up, which is done by lazy-lsp just below. See
--   https://github.com/folke/neodev.nvim#-setup
require('neodev').setup()
require('which-key').setup {
  window = { padding = { 1, 2, 1, 8 } }
}
require('lazy-lsp').setup {
  excluded_servers = {
    "efm",
    "diagnosticls"
  },
  default_config = {
    on_attach = function(client, buffnr)
      local wk = require('which-key')
      wk.register({["<leader>l"] = { name = "lsp" } }, { buffer = buffnr })
      vim.keymap.set("n", "<leader>lr", "<cmd>Telescope lsp_references<cr>", { desc = "references", buffer = buffnr })
      vim.keymap.set("n", "<leader>ld", "<cmd>lua vim.lsp.buf.definition()<cr>", { desc = "definition", buffer = buffnr })
    end,
  },
}
require('nvim-treesitter.configs').setup {
  highlight = { enable = true },
  playground = { enable = true },
  textobjects = {
    select = {
      enable = true,
      keymaps = {
        ["af"] = "@function.outer",
        ["if"] = "@function.inner",
        ["ac"] = "@class.outer",
        ["ic"] = "@class.inner",
      },
    },
  },
}
require('nvim-cursorline').setup{
  cursorline = {
    enable = true,
    number = true,
  },
  cursorword = { enable = false },
}

require('telescope').load_extension('fzf');

require('config.map')
