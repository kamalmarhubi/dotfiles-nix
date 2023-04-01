# nix flake + home-manager based dotfiles

To bootstrap, [install nix] and run:

    nix run github:nix-community/home-manager -- \
      switch --impure --flake github:kamalmarhubi/dotfiles-nix#bootstrap

optionally with `--dry-run` or `--verbose` as well.

The `--impure` is needed because the bootstrap config reads from the
environment, which is disallowed by default when using flakes.

Finally add an entry to the `homeConfigurations` output so that

    home-manager switch

works.

[install nix]: https://zero-to-nix.com/start/install

## Note for non-kamals

Although the above instructions may work for you, I am not responsible for any
damage to your $HOME.
