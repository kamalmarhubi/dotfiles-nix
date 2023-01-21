return {
  {
    "nvim-treesitter/nvim-treesitter",
    build = ":TSUpdate",
    event = "BufReadPost",
    dependencies = {
      "nvim-treesitter/nvim-treesitter-textobjects",
    },
    config = function()
      require("nvim-treesitter.configs").setup({
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

        ensure_installed = {
          "bash",
          "beancount",
          "c",
          "cmake",
          "cpp",
          "css",
          "diff",
          "dockerfile",
          "dot",
          "fennel",
          "fish",
          "git_rebase",
          "gitattributes",
          "gitcommit",
          "gitignore",
          "go",
          "gomod",
          "graphql",
          "hcl",
          "help",
          "html",
          "http",
          "java",
          "javascript",
          "jq",
          "json",
          "jsonc",
          "kotlin",
          "lua",
          "make",
          "nix",
          "perl",
          "proto",
          "python",
          "query",
          "rst",
          "rust",
          "sql",
          "swift",
          "terraform",
          "toml",
          "tsx",
          "typescript",
          "vim",
          "yaml",
        },
      })
    end,
  }
}