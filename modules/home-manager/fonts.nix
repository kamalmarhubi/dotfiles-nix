{pkgs, ...}: {
  fonts.fontconfig.enable = true;
  home.packages = with pkgs; [
    aporetic
    (iosevka-bin.override {variant = "SGr-IosevkaFixed";})
    (iosevka-bin.override {variant = "SGr-IosevkaFixedSlab";})
    nerd-fonts.symbols-only
  ];
}
