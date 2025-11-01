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
  
  # Kanata config
  xdg.configFile."kanata/kanata.kbd".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/kanata/kanata.kbd";
}
