{
  config,
  pkgs,
  ...
}: {
  home = {
    shellAliases = {
      git = "git-branchless wrap --";
    };

    packages = with pkgs; [
      delta
      gh
      git
      git-absorb
      git-lfs
      git-branchless
      jujutsu
      mine.jj-hunk
      # git-filter-repo
      lazygit
    ];
    file.".local/bin/jj-gh-fork".source =
      config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/jj/jj-gh-fork";
    file.".local/bin/jj-grove".source =
      config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/jj/jj-grove";
  };

  xdg.configFile."jj/config.toml".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/jj/config.toml";
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
  xdg.configFile."git/config.gh".text = ''
    [credential "https://github.com"]
    	helper =
    	helper = !${pkgs.gh}/bin/gh auth git-credential
    [credential "https://gist.github.com"]
    	helper =
    	helper = !${pkgs.gh}/bin/gh auth git-credential
  '';
}
