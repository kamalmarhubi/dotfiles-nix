{pkgs, ...}: {
  programs.fish.enable = true;

  home.packages = with pkgs; [
    _1password
    atool
    entr
    fd
    graphicsmagick
    magic-wormhole
    mtr
    nushell
    poetry
    pstree
    pv
    ripgrep
    tree
    wget
  ];

  # For whatever reason, the installer didn't put this somewhere that
  # fish would see. Since the nix-daemon.fish file guards against
  # double-sourcing, there's no harm including this in all systems.
  xdg.configFile."fish/conf.d/nix.fish".text = ''
    # Nix
    if test -e '/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.fish'
      . '/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.fish'
    end
    # End Nix
  '';
}
