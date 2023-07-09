{pkgs, inputs, ...}: {
  programs.fish.enable = true;

  home.packages = let
    dig-pretty = pkgs.python3Packages.buildPythonApplication {
          name = "dig-pretty";
          src = inputs.dig-pretty;
          format = "pyproject";
          buildInputs = [pkgs.python3Packages.setuptools];
          propagatedBuildInputs = with pkgs.python3Packages; [ pyyaml ];
        };
        in
  with pkgs; [
    _1password
    atool
    cargo
    dig  # Needed because on darwin there's no +yaml
    dig-pretty
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

  # home.file.".local/bin/dig-pretty".source = "${inputs.dig-pretty}/dig-pretty.py";

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
