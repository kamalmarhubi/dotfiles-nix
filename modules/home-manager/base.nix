{
  pkgs,
  inputs,
  config,
  ...
}: {
  home.packages = with pkgs; [
    nur.repos.gigamonster256.ghostty-darwin
    _1password-cli
    atool
    bun
    cargo
    entr
    fd
    ffmpeg
    graphicsmagick
    gron
    jdk
    kanata
    magic-wormhole
    mtr
    nushell
    pipx
    poetry
    pstree
    pv
    ripgrep
    shellcheck
    tree
    trippy
    wget
  ];

  nixpkgs.allowUnfreePackages = with pkgs; [
    _1password-cli
  ];

  xdg.enable = true;

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
  
  # Kanata config
  xdg.configFile."kanata/kanata.kbd".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/kanata/kanata.kbd";
}
