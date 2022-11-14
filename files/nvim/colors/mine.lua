vim.g.colors_name = "mine"

local lush = require('lush')
local hsluv = lush.hsluv
local neobones = require('neobones')

local spec = lush.extends({neobones}).with(function()
  return {
    DevIconMd { fg = hsluv(0, 0, 60) },
  }
end)

lush(spec)
