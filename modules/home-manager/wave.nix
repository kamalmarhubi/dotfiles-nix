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
    (unstable.gws.overrideAttrs (old: rec {
      version = "0.19.0";
      src = pkgs.fetchFromGitHub {
        owner = "googleworkspace";
        repo = "cli";
        rev = "v${version}";
        hash = "sha256-r1BrDoZ3EzSW/CGLjuOsCeMRnZTzpcaIP+snQfsuXxc=";
      };
      cargoDeps = pkgs.rustPlatform.fetchCargoVendor {
        inherit src;
        hash = "sha256-3/gK5Y2VD5azxIhjzvqYT88eYwh+zmgjGIKJrXdu6jw=";
      };
    }))
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
