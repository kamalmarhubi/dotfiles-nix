{
  config,
  pkgs,
  ...
}: {
  home = {
    shellAliases = {
      git = "git-branchless wrap --";
    };

    packages =
      with pkgs; [
        delta
        gh
        git
        git-absorb
        git-lfs
        git-branchless
        llm-agents.hunk
        unstable.jujutsu
        mine.jj-hunk
        # git-filter-repo
        lazygit
      ];
    file.".local/bin/jj-gh-fork".source =
      config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/jj/jj-gh-fork";
    file.".local/bin/jj-try".source =
      config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/jj/jj-try";
    file.".local/bin/jj-grove".source =
      config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/jj/jj-grove";
    file.".local/bin/jj-stack".source =
      config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/jj/jj-stack";
  };

  xdg.configFile."fish/completions/jj.fish".source =
    config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/fish/completions/jj.fish";
  xdg.configFile."jj/config.toml".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/jj/config.toml";
  xdg.configFile."git/config".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/git/config";
  xdg.configFile."git/ignore".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/git/ignore";
  xdg.configFile."hunk/config.toml".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/hunk/config.toml";
  xdg.configFile."git/config.mine".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/git/config.mine";
  xdg.dataFile."gh/extensions/gh-stack".source = "${pkgs.unstable.gh-stack}/bin";
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
