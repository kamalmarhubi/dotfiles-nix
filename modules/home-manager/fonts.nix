{pkgs, ...}: {
  fonts.fontconfig.enable = true;
  home.packages = with pkgs; [
    aporetic
    (iosevka-bin.override {variant = "SGr-IosevkaFixed";})
    (iosevka-bin.override {variant = "SGr-IosevkaFixedSlab";})
    # TODO: Go to unstable once codicons 0.0.46 lands with gemini and kimi icons.
    master.nerd-fonts.symbols-only # from master for claude / openai icons
  ];
}
