{
  config,
  pkgs,
  inputs,
  system,
  ...
}: {
  home = {
    shellAliases = {
      git = "git-branchless wrap --";
    };

    packages = with pkgs; [
      delta
      git
      git-absorb
      git-lfs
      git-branchless
      # TODO(25.11): Consider going back to stable?
      unstable.jujutsu # 0.34 is latest; stable nixpkgs-25.05-darwin is on 0.29
      # git-filter-repo
      lazygit
    ];
  };

  xdg.configFile."jj/config.toml".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/jjconfig.toml";
  xdg.configFile."git/config".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/git/config";
  xdg.configFile."git/config.mine".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/git/config.mine";
  xdg.configFile."git/config.system".text = let
    credentialHelper =
      if pkgs.stdenv.isLinux
      then "libsecret"
      else if pkgs.stdenv.isDarwin
      then "osxkeychain"
      else "";
  in ''
    [credential]
    	helper = ${credentialHelper}
  '';
}
