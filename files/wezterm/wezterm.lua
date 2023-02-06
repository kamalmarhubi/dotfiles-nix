local wezterm = require 'wezterm'

return {
  default_prog = { wezterm.home_dir .. '/.nix-profile/bin/fish', '--login', '--interactive'},
  font = wezterm.font 'Iosevka Fixed Slab',
  font_size = 14,
  color_scheme = "tokyonight-day",
}
