# nix flake + home-manager based dotfiles

To bootstrap, install nix and then run:

    nix --extra-experimental-features 'nix-command flakes' run \
      'github:kamalmarhubi/dotfiles-nix#homeConfigurations.genericLinux.activationPackage'

or for macos:

    nix --extra-experimental-features 'nix-command flakes' run \
      'github:kamalmarhubi/dotfiles-nix#homeConfigurations.genericMacos.activationPackage'
