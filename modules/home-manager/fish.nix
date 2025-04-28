{config, pkgs, ...}: {
  programs.fish.enable = true;
  # TODO(25.05): Remove this; temporary to get fish 4.0 for built-in OSC
  #              stuffs.
  programs.fish.package = pkgs.unstable.fish;

  programs.starship = {
    enable = true;
    enableTransience = true;
  };

  # For whatever reason, the installer didn't put this somewhere that
  # fish would see. Since the nix-daemon.fish file guards against
  # double-sourcing, there's no harm including this in all systems.
  xdg.configFile."fish/conf.d/nix.fish".text = ''
    # Nix
    if test -e '/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.fish'
      . '/nix/var/nix/profiles/default/etc/profile.d/nix-daemon.fish'
    end
    # End Nix
  '';
  xdg.configFile."starship.toml".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/starship.toml";
}
