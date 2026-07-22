{
  pkgs,
  inputs,
  config,
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
    # TODO(NixOS/nixpkgs#522307): Drop this override after the issue is resolved.
    # packaging 26 normalizes direct-reference spacing differently than these
    # pipx 1.8.0 tests expect.
    (pipx.overridePythonAttrs (old: {
      disabledTests =
        (old.disabledTests or [])
        ++ [
          "test_fix_package_name"
          "test_parse_specifier_for_metadata"
        ];
    }))
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
  xdg.configFile."kanata/kanata.kbd".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/kanata/kanata.kbd";
}
