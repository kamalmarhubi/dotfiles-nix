# Remaining:
# - fix up repo to not be vcsh repo
#   - move .config/nixpkgs/* to root
#   - add README with instructions on how to run nix
# - manage ~/.config/nix/nix.conf
# - add genericLinux and genericMacos configs?
#   - assign kamal@kx = genericLinux?
# - start using it at work
{
  description = "Kamal pretends to use nix?";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { nixpkgs, home-manager, ... }: {
    homeConfigurations = {
      "kamal@kx7" = let
        system = "x86_64-linux";
        pkgs = nixpkgs.legacyPackages.${system};
        repoUrl = "https://github.com/kamalmarhubi/dotfiles-nix.git";
      in
      home-manager.lib.homeManagerConfiguration {
        inherit pkgs;

        # Specify your home configuration modules here, for example,
        # the path to your home.nix.
        modules = [
	  ./dotfiles-nix.nix
	  {
            programs = {
              home-manager.enable = true;
              fish.enable = true;
              neovim = {
                enable = true;
                plugins = with pkgs.vimPlugins; [
                ];
              };
            };

            home = rec {
              username = "kamal";
              homeDirectory = "/home/kamal";
              stateVersion = "22.11";

              sessionVariables = {
                EDITOR = "nvim";
              };
            };
          }
        ];
      };
    };
  };
}
