{
  config,
  pkgs,
  ...
}: {
  home = {
    # shellAliases = {
    #   git = "git-branchless wrap --";
    # };

    packages = with pkgs; [
      delta
      git
      # git-filter-repo
      jujutsu
      git-branchless
      sapling
    ];
  };

  xdg.configFile."git/config".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.dataHome}/dotfiles-nix/files/git/config";
  xdg.configFile."git/config.mine".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.dataHome}/dotfiles-nix/files/git/config.mine";
  xdg.configFile."git/config.system".text = let
    credentialHelper =
      if pkgs.stdenv.isLinux then "libsecret"
      else if pkgs.stdenv.isDarwin then "osxkeychain"
      else "";
  in ''
  [credential]
  	helper = ${credentialHelper}
  '';
}
