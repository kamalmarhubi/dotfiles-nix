{pkgs, ...}: {
  fonts.fontconfig.enable = true;
  home.packages = with pkgs; [
    (iosevka-bin.override {variant = "SGr-IosevkaFixed";})
    (iosevka-bin.override {variant = "SGr-IosevkaFixedSlab";})
    (nerdfonts.override {fonts = ["NerdFontsSymbolsOnly"];})
  ];
}
