{
  pkgs,
  inputs,
  config,
  ...
}: {
  home.packages = with pkgs; [
    nur.repos.AusCyber.ghostty-bin
    _1password-cli
    atool
    bun
    entr
    fd
    ffmpeg
    graphicsmagick
    gron
    jdk
    kanata
    magic-wormhole
    (pkgs.runCommand "moreutils-selected" {} ''
      mkdir -p $out/bin
      ln -s ${moreutils}/bin/vipe $out/bin/
      ln -s ${moreutils}/bin/sponge $out/bin/
    '')
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
