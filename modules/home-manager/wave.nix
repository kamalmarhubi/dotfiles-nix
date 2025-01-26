{
  config,
  pkgs,
  lib,
  ...
}: let
  terraformPluginCache = "${config.xdg.cacheHome}/terraform/plugin-cache";
in {
  home.packages = with pkgs; [
    argocd
    asdf-vm
    awscli2
    circleci-cli
    cmake
    cmctl
    colima
    colordiff
    crane
    git-lfs
    go
    k9s
    (wrapHelm kubernetes-helm {plugins = [kubernetes-helmPlugins.helm-diff];})
    kustomize
    ninja
    pgcli
    pgformatter
    steampipe
    vault
    yaml2json
  ];

  nixpkgs.allowUnfreePackages = with pkgs; [
    vault
  ];

  xdg.configFile."git/config.local".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/git/config.wave";
  home.file.".terraformrc".text = ''
    # Temporarily disabled because of bad interaction with lock files.
    # Related: https://github.com/hashicorp/terraform/issues/29958#issuecomment-1190245494
    # plugin_cache_dir = "${terraformPluginCache}"
  '';
  home.activation.ensureTerraformPluginCache = lib.hm.dag.entryAfter ["writeBoundary"] ''
    $VERBOSE_ECHO "Ensuring terraform plugin cache directory exists"

    $DRY_RUN_CMD mkdir -p "${terraformPluginCache}"
  '';
}
