vim.opt.shortmess:append("I")
vim.opt.termguicolors = true
vim.opt.background = "light"
vim.opt.showmode = false  -- turn this off so gitlinker messages show in visual mode
vim.opt.smartcase = true
vim.opt.ignorecase = true
vim.cmd.colorscheme('tokyonight-day')
vim.opt.cursorline = true

vim.opt.timeoutlen = 500

vim.opt.formatoptions:append("r")  -- respect comment on newline in insert
vim.opt.formatoptions:append("o")  -- respect comment on o in normal

-- Super handy command mode abbreviation for the directory of the current file.
-- From
--   http://vim.wikia.com/wiki/Easy_edit_of_files_in_the_same_directory#Using_a_command_line_abbreviation
vim.cmd[[cabbr <expr> %% expand('%:p:h')]]

-- Both of these need to happen before lspconfig is set up, which is done by lazy-lsp just below. See
--   https://github.com/folke/neoconf.nvim#-setup
--   https://github.com/folke/neodev.nvim#-setup
require('neoconf').setup()
require('neodev').setup()
require('lazy-lsp').setup {
  -- Should probably switch to explicitly listing ones I want?
  excluded_servers = {
    "diagnosticls",
    "efm",
    "jedi_language_server",
    "ltex",
    "pylsp",
    "sqls",
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

require('leap').add_default_mappings()
require('Comment').setup()
require('guess-indent').setup()
require('nvim-surround').setup()
require('which-key').setup {
  window = { padding = { 1, 2, 1, 8 } }
}
require("indent_blankline").setup {
  char = "▏",
  -- char = "▎",
  space_char_blankline = " ",
  show_current_context = true,
  -- show_current_context_start = true,
}
require('gitlinker').setup()

require('telescope').load_extension('fzf')

require('possession').setup {
  autosave = {
    current = true,
    tmp = false,
    on_load = true,
    on_quit = true,
  },
  plugins = {
    -- Keep hidden buffers...
    delete_hidden_buffers = false,
    -- ... but then we need to close all buffers before session load.
    delete_buffers = true,
  },
}
require('telescope').load_extension('possession')
require('toggleterm').setup {
  open_mapping = [[<c-\>]],
  direction = "float",
  shade_terminals = true,
  shading_factor = 0.75,
  shell = "fish",
}
local termgroup = vim.api.nvim_create_augroup("kterm", { clear = true })
vim.api.nvim_create_autocmd("TermOpen", {
  group = termgroup,
  callback = function()
    vim.opt_local.cursorline = false
  end,
})


require('config.map')
