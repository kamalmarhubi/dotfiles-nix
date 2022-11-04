local wk = require('which-key')

-- Clear search highlight
vim.keymap.set('n', '<esc>', '<cmd>nohlsearch<cr>', { desc = "clear search highlight", silent = true })
vim.keymap.set('n', '<leader><leader>', '<cmd>nohlsearch<cr>', { desc = "clear search highlight", silent = true })
wk.register({["<leader><leader>"] = "which_key_ignore" })

wk.register({["<leader>f"] = { name = "find" } })
vim.keymap.set("n", "<leader>ff", "<cmd>Telescope find_files<cr>", { desc = "find file" })
vim.keymap.set("n", "<leader>fg", "<cmd>Telescope live_grep<cr>", { desc = "grep" })
vim.keymap.set("n", "<leader>fr", "<cmd>Telescope oldfiles<cr>", { desc = "recent files" })
vim.keymap.set("n", "<leader>fb", "<cmd>Telescope buffers<cr>", { desc = "buffers" })

-- Ideally these should be set conditionally... maybe in lsp on_attach?
wk.register({["<leader>l"] = { name = "lsp" } })
vim.keymap.set("n", "<leader>lr", "<cmd>Telescope lsp_references<cr>", { desc = "references" })
vim.keymap.set("n", "<leader>ld", "<cmd>lua vim.lsp.buf.definition()<cr>", { desc = "definition" })
