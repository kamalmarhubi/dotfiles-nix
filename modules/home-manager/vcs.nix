{
  config,
  dotFilesNixHomeManagerInstallationType,
  pkgs,
  ...
}: let
  dotFilesNixDirName =
    if dotFilesNixHomeManagerInstallationType == "standalone"
    then "home-manager"
    else "dotfiles-nix";
in {
  home = {
    shellAliases = {
      git = "git-branchless wrap --";
    };

    sessionPath = ["${config.home.homeDirectory}/.local/bin/.dotfiles-nix"];

    packages =
      with pkgs; [
        delta
        gh
        git
        git-absorb
        git-lfs
        git-branchless
        llm-agents.hunk
        jujutsu
        mine.jj-hunk
        # git-filter-repo
        lazygit
      ];
    file.".local/bin/.dotfiles-nix".source =
      config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/${dotFilesNixDirName}/files/bin";
  };

  xdg.configFile."fish/completions/jj.fish".source =
    config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/fish/completions/jj.fish";
  xdg.configFile."jj/config.toml".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/jj/config.toml";
  xdg.configFile."git/config".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/git/config";
  xdg.configFile."git/ignore".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/git/ignore";
  xdg.configFile."hunk/config.toml".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/hunk/config.toml";
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
