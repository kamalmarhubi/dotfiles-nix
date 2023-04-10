{pkgs, ...}: {
  home.packages = with pkgs; [
    iina
  ];
}
