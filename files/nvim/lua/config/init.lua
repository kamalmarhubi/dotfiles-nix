-- Super handy command mode abbreviation for the directory of the current file.
-- From
--   http://vim.wikia.com/wiki/Easy_edit_of_files_in_the_same_directory#Using_a_command_line_abbreviation
vim.cmd[[cabbr <expr> %% expand('%:p:h')]]

import('leap', function(m) m.add_default_mappings() end)
import('Comment', function(m) m.setup() end)
import('guess-indent', function(m) m.setup() end)
import('nvim-surround', function(m) m.setup() end)
import('lazy-lsp', function(m) m.setup() end)
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

vim.opt.termguicolors = true
vim.cmd.colorscheme('acme')
