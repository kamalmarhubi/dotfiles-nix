# nix flake + home-manager based dotfiles

To bootstrap, install nix and run:

    nix --extra-experimental-features 'nix-command flakes' build --impure --expr \
      "(builtins.getFlake ''github:kamalmarhubi/dotfiles-nix'').homeFor {
         system = builtins.currentSystem;
         username = ''$USER'';
         homeDirectory = ''$HOME'';
      }"

then running the generated

    result/activate

possibly with `DRY_RUN=1 VERBOSE=1` in the environment to see what it'll do.

Finally add an entry to the `homeConfigurations` output so that

    home-manager switch

works.


## Note for non-kamals

Although the above instructions may work for you, I am not responsible for any
damage to your $HOME.
