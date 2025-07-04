{
  config,
  pkgs,
  inputs,
  system,
  ...
}: {
  home.packages = with pkgs; [
    (
      if stdenv.isDarwin
      then emacs30-macport
      else emacs
    )
  ];
  xdg.configFile."emacs".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/emacs";
}
