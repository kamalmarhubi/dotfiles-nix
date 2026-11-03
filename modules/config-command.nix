{
  lib,
  pkgs,
  manager,
  managerCmd,
  dotfilesNixDir,
}: {
  options = {
    flake = lib.mkOption {
      type = lib.types.str;
      description = "Absolute local checkout path followed by #configuration-name.";
    };
    extraArgs = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [];
      description = "Additional arguments passed to the configuration command.";
    };
  };

  package = cfg:
    pkgs.writeShellApplication {
      name = "config";
      runtimeInputs = [pkgs.coreutils];
      extraShellCheckFlags = ["--external-sources"];
      text = ''
        manager=${lib.escapeShellArg manager}
        manager_cmd=${lib.escapeShellArg managerCmd}
        flake=${lib.escapeShellArg cfg.flake}
        extra_args=(${lib.escapeShellArgs cfg.extraArgs})
        implementation=${lib.escapeShellArg "${dotfilesNixDir}/files/libexec/config"}

        if [[ ! -r "$implementation" ]]; then
          printf 'config: live implementation not found or unreadable: %s\n' "$implementation" >&2
          exit 1
        fi

        # Check the build-time snapshot, but run the live checkout copy.
        # shellcheck source=${../files/libexec/config}
        source "$implementation"
      '';
    };
}
