# home-manager, nix-darwin, flakes, &c

A flake that contains dotfiles managed by home-manager, either standalone or as
nix-darwin module (together with additional system config).


## bootstrap

### just home-manager

To bootstrap a standalone home-manager setup, [install nix] and run:

    nix run github:nix-community/home-manager/release-23.11 -- \
      switch --impure --flake github:kamalmarhubi/dotfiles-nix#bootstrap

optionally with `--dry-run` or `--verbose` as well.

The `--impure` is needed because the bootstrap config reads from the
environment, which is disallowed by default when using flakes.

Finally add an entry to the `homeConfigurations` output so that

    home-manager switch

works.

### nix-darwin & home-manager

To bootstrap a nix-darwin & home-manager setup, [install nix] and run:

    nix run github:LnL7/nix-darwin -- \
      switch --impure --flake github:kamalmarhubi/dotfiles-nix#bootstrap

optionally with `--dry-run` or `--verbose` as well.

The `--impure` is needed because the bootstrap config reads the current system
environment, which is disallowed by default when using flakes.

Finally add an entry to the `darwinConfigurations` output so that

    darwin-rebuild switch --flake ~/.config/dotfiles-nix

works.

[install nix]: https://zero-to-nix.com/start/install

## Note for non-kamals

Although the above instructions may work for you, I am not responsible for any
damage to your $HOME.
