import('leap', function(m) m.add_default_mappings() end)
import('Comment', function(m) m.setup() end)
import('guess-indent', function(m) m.setup() end)
import('nvim-surround', function(m) m.setup() end)
import('lazy-lsp', function(m) m.setup() end)
vim.opt.termguicolors = true
vim.cmd.colorscheme('acme')
