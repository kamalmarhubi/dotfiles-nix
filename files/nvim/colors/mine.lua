vim.g.colors_name = "mine"

package.loaded['config.colors'] = nil
require('lush')(require('config.colors'))
