return {
  "echasnovski/mini.animate",
  opts = function(_, opts)
    local animate = require("mini.animate")
    opts.cursor = {
      timing = animate.gen_timing.cubic({ easing = "out", duration = 150, unit = "total" }),
    }
  end,
}
