return {
  {
    "nvim-neotest/neotest",
    -- lazy = true,
    requires = {
      "nvim-lua/plenary.nvim",
      "nvim-treesitter/nvim-treesitter",
    },
    config = function()
      require("neotest").setup({
        adapters = {
          require("neotest-python"),
          require("neotest-go"),
        },
      })
    end,
  },
  -- {"nvim-neotest/neotest-python", lazy = true},
  -- {"akinsho/neotest-go", lazy = true},
  "nvim-neotest/neotest-python",
  "nvim-neotest/neotest-go",
}
