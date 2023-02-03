{ pkgs, ... }: {
  fonts.fontconfig.enable = true;
  home.packages = with pkgs; [
    (nerdfonts.override {fonts = ["NerdFontsSymbolsOnly"];})
    (iosevka-bin.override {variant = "sgr-iosevka-fixed";})
    (iosevka-bin.override {variant = "sgr-iosevka-fixed-slab";})
  ];
}
