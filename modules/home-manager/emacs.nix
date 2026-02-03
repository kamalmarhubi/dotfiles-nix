{
  config,
  pkgs,
  ...
}: let
  emacs =
    if pkgs.stdenv.isDarwin
    then pkgs.emacs-macport
    else pkgs.emacs;
in {
  home.packages = [
    ((pkgs.emacsPackagesFor emacs).emacsWithPackages (epkgs: [
      epkgs.treesit-grammars.with-all-grammars
    ]))
  ];
  xdg.configFile."emacs".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/emacs";
}
