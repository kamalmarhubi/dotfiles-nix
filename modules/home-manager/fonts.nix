{pkgs, ...}: {
  fonts.fontconfig.enable = true;
  home.packages = with pkgs; [
    # TODO: When upgrading to 24.05, switch.
    # (iosevka-bin.override {variant = "SGr-IosevkaFixed";})
    # (iosevka-bin.override {variant = "SGr-IosevkaFixedSlab";})
    (iosevka-bin.override {variant = "sgr-iosevka-fixed";})
    (iosevka-bin.override {variant = "sgr-iosevka-fixed-slab";})
    (nerdfonts.override {fonts = ["NerdFontsSymbolsOnly"];})
  ];
}
