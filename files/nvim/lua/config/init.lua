-- Super handy command mode abbreviation for the directory of the current file.
-- From
--   http://vim.wikia.com/wiki/Easy_edit_of_files_in_the_same_directory#Using_a_command_line_abbreviation
vim.g.mapleader = ' '
vim.o.timeoutlen = 500
vim.cmd[[cabbr <expr> %% expand('%:p:h')]]

import('leap', function(m) m.add_default_mappings() end)
import('Comment', function(m) m.setup() end)
import('guess-indent', function(m) m.setup() end)
import('nvim-surround', function(m) m.setup() end)
import('lazy-lsp', function(m) m.setup {
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
end)
import('nvim-treesitter.configs', function(m) m.setup {
  highlight = { enable = true },
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
end)

import('config.map')

vim.opt.termguicolors = true
vim.cmd.colorscheme('acme')
