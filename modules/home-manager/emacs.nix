{
  config,
  pkgs,
  ...
}: {
  home.packages = with pkgs; [
    (
      if stdenv.isDarwin
      # TODO(25.11): Switch back to stable once it's released.
      then unstable.emacs-macport
      else emacs
    )
  ];
  xdg.configFile."emacs".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/emacs";
}
