{
  pkgs,
  inputs,
  config,
  dotfilesNixDir,
  ...
}: {
  home.packages = with pkgs; [
    ghostty-bin
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
      ln -s ${moreutils}/bin/sponge $out/bin/
      ln -s ${moreutils}/bin/ts $out/bin/
      ln -s ${moreutils}/bin/vipe $out/bin/
    '')
    mtr
    nushell
    # TODO(26.11): Go back to stable pipx after upgrading nixpkgs.
    unstable.pipx
    poetry
    pstree
    pv
    ripgrep
    shellcheck
    tree
    trippy
    wget
  ];

  home.sessionPath = ["${config.home.homeDirectory}/.local/bin"];

  nixpkgs.allowUnfreePackages = with pkgs; [
    _1password-cli
  ];

  xdg.enable = true;

  # Kanata config
  xdg.configFile."kanata/kanata.kbd".source = config.lib.file.mkOutOfStoreSymlink "${dotfilesNixDir}/files/kanata/kanata.kbd";
}
