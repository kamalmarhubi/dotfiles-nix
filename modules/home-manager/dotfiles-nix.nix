{dotfilesNixDir, ...}: {
  home.checkouts.dotfiles-nix = {
    path = dotfilesNixDir;
    url = "https://github.com/kamalmarhubi/dotfiles-nix.git";
    acceptedOrigins = [
      "git@github.com:kamalmarhubi/dotfiles-nix.git"
      "ssh://git@github.com/kamalmarhubi/dotfiles-nix.git"
    ];
  };

  xdg.configFile."nix/nix.conf".text = ''
    experimental-features = nix-command flakes
  '';
}
