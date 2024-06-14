{
  config,
  pkgs,
  lib,
  inputs,
  system,
  ...
}: let
  terraformPluginCache = "${config.xdg.cacheHome}/terraform/plugin-cache";
in {
  home.packages = with pkgs; [
    argocd
    asdf-vm
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
    # Current vagrant doesn't build because of a build failure in grpc that may
    # be related to https://github.com/grpc/grpc/issues/35148
    #
    # Could probably try to fix it by setting
    #   BUNDLE_BUILD__GRPC=--with-cflags=-Wno-error=incompatible-function-pointer-types
    # in the environment?
    #
    # (At least chatgpt thinks so...)
    inputs.nixpkgs-2305.legacyPackages.${system}.vagrant
    yaml2json
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
