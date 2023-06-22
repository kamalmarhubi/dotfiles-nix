local wezterm = require 'wezterm'

return {
  default_prog = { wezterm.home_dir .. '/.nix-profile/bin/fish', '--login', '--interactive'},
  font = wezterm.font 'Iosevka Fixed Slab',
  font_size = 13,
  color_scheme = "tokyonight-day",
  -- Default plus  ▏which is used in nvim borders & indent guides
  -- Default: https://wezfurlong.org/wezterm/config/lua/config/selection_word_boundary.html
  selection_word_boundary = " │\t\n{}[]()\"'`",
}
