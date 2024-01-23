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
      then emacsMacport
      else emacs
    )
  ];
}
