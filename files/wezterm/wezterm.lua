local wezterm = require 'wezterm'

return {
  default_prog = { wezterm.home_dir .. '/.nix-profile/bin/fish', '--login', '--interactive'},
  font = wezterm.font 'Iosevka Fixed Slab',
  font_size = 13,
  use_cap_height_to_scale_fallback_fonts = true,
  color_scheme = "tokyonight-day",
  -- Default plus
  --   ▏    used in nvim borders & indent guides
  --   ─    used in tree output
  -- Default: https://wezfurlong.org/wezterm/config/lua/config/selection_word_boundary.html
  selection_word_boundary = " │─\t\n{}[]()\"'`",
}
