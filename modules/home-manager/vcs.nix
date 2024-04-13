{
  config,
  pkgs,
  inputs,
  system,
  ...
}: let
  configDir =
    if pkgs.stdenv.hostPlatform.isDarwin
    then "Library/Application Support"
    else config.xdg.configHome;
in {
  home = {
    shellAliases = {
      git = "git-branchless wrap --";
    };

    packages = with pkgs; [
      delta
      git
      git-lfs
      # git-filter-repo
      inputs.jj.outputs.packages.${system}.jujutsu
      inputs.git-branchless.outputs.packages.${system}.git-branchless
      inputs.git-branchless.outputs.packages.${system}.scm-diff-editor
      lazygit
      sapling
    ];
  };

  home.file."${configDir}/jj/config.toml".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/jjconfig.toml";
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
