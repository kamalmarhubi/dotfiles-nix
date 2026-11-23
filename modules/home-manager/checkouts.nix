{
  config,
  lib,
  pkgs,
  ...
}: {
  options.home.checkouts = lib.mkOption {
    default = {};
    description = "Local checkouts to validate when present and clone when missing. Existing checkouts are never updated or converted to jj.";
    type = lib.types.attrsOf (lib.types.submodule {
      options = {
        path = lib.mkOption {
          type = lib.types.strMatching "/.*";
          description = "Absolute path to the canonical checkout directory.";
        };
        url = lib.mkOption {
          type = lib.types.str;
          description = "URL used by jj git clone, also accepted as an existing origin.";
        };
        acceptedOrigins = lib.mkOption {
          type = lib.types.listOf lib.types.str;
          default = [];
          description = "Additional exact origin URLs to accept without rewriting the remote.";
        };
      };
    });
  };

  config.home.activation = lib.mkMerge (lib.mapAttrsToList (name: checkout: let
      check = pkgs.writeShellApplication {
        name = "check-checkout";
        runtimeInputs = [pkgs.git];
        text = ''
          die() {
            echo "$@" >&2
            exit 1
          }
          target_dir=${lib.escapeShellArg checkout.path}

          test ! -L "$target_dir" || die "$target_dir is a symlink; expected the canonical checkout directory"
          if [ ! -e "$target_dir" ]; then
            $VERBOSE_ECHO "$target_dir does not exist; it will be cloned"
            exit 0
          fi
          test -d "$target_dir" || die "$target_dir exists but is not a directory"
          test "$(git -C "$target_dir" rev-parse --show-toplevel 2>/dev/null)" = "$target_dir" \
            || die "$target_dir exists but is not the checkout root"
          origin="$(git -C "$target_dir" config --local --get remote.origin.url 2>/dev/null || true)"
          case "$origin" in
            ${lib.concatMapStringsSep "|" lib.escapeShellArg ([checkout.url] ++ checkout.acceptedOrigins)}) ;;
            *) die "$target_dir has an unexpected origin: $origin" ;;
          esac
          $VERBOSE_ECHO "$target_dir looks good!"
        '';
      };
      clone = pkgs.writeShellApplication {
        name = "clone-checkout";
        runtimeInputs = [pkgs.jujutsu pkgs.git];
        text = ''
          target_dir=${lib.escapeShellArg checkout.path}
          if [ -e "$target_dir" ]; then
            $VERBOSE_ECHO "$target_dir exists; skipping clone"
            exit 0
          fi
          $DRY_RUN_CMD jj git clone ${lib.escapeShellArg checkout.url} "$target_dir"
        '';
      };
    in {
      "checkCheckout-${name}" = lib.hm.dag.entryBefore ["writeBoundary"] ''
        ${check}/bin/check-checkout
      '';
      "zzzcloneCheckout-${name}" = lib.hm.dag.entryAfter ["writeBoundary"] ''
        ${clone}/bin/clone-checkout
      '';
    })
    config.home.checkouts);
}
