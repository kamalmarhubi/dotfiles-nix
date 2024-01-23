{pkgs, ...}: {
  fonts.fontconfig.enable = true;
  home.packages = with pkgs; [
    (iosevka-bin.override {variant = "sgr-iosevka-fixed";})
    (iosevka-bin.override {variant = "sgr-iosevka-fixed-slab";})
    (nerdfonts.override {fonts = ["NerdFontsSymbolsOnly"];})
  ];
}
