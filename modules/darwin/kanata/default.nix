{ config, lib, pkgs, ... }:

with lib;

let
  cfg = config.services.kanata;
in

{
  /*
  IMPORTANT: This service runs as a system daemon (root) to access HID devices.
  This means keyboard remapping affects ALL users system-wide, not just the
  current user. For multi-user systems, this may need rethinking - other users
  will experience the same key remapping when they log in.
  
  Alternative approaches for multi-user systems might include:
  - User-specific daemons that start/stop on login/logout
  - Conditional service enabling based on active user
  - Using kanata's TCP server mode for per-user configs

  TODO: Restrict network access to loopback only using SandboxProfile
  To prevent kanata from accessing the internet while still allowing its TCP
  server feature to work on localhost:
  - Add SandboxProfile key to launchd serviceConfig
  - Use sandbox profile to allow loopback network (127.0.0.1, ::1)
  - Explicitly deny external network access (network-outbound to remote IPs)
  - Ensure IOKit access remains allowed for keyboard/input device operations
  - Test that input monitoring and keyboard remapping still work correctly
  Note: The sandbox might interfere with IOKit operations; if so, use PF
  (packet filter) rules instead to restrict network without sandboxing other
  system access.
  */
  
  options.services.kanata = {
    enable = mkEnableOption "kanata keyboard remapper";
    package = mkPackageOption pkgs "kanata" { };
    configFile = mkOption {
      type = types.path;
      description = "Path to kanata configuration file";
    };
    keepAlive = mkOption {
      type = types.bool;
      default = true;
      description = ''
        Whether to automatically restart kanata if it exits.
        Set to false for easier development/testing where you want
        manual control over when the service runs.
      '';
    };
  };

  config = mkIf cfg.enable {
    # Ensure the virtual HID device driver is available
    # Use the darwinDriver from kanata package for automatic version coordination
    services.karabiner-driverkit-virtualhiddevice = {
      enable = mkDefault true;
      package = mkDefault cfg.package.darwinDriver;
    };

    environment.systemPackages = [ cfg.package ];

    # Copy kanata to stable path for TCC permissions
    # macOS TCC permissions are path-specific, so we need a stable path
    # that doesn't change when nix store paths change
    system.activationScripts.postActivation.text = ''
      echo "copying kanata to /Library/Application Support/org.nixos.kanata/ for stable TCC permissions..." >&2
      mkdir -p "/Library/Application Support/org.nixos.kanata"
      cp -f "${lib.getExe cfg.package}" "/Library/Application Support/org.nixos.kanata/kanata"
      chmod +x "/Library/Application Support/org.nixos.kanata/kanata"
    '';

    # Run as system daemon to access HID devices (requires root permissions)
    launchd.daemons.kanata = {
      serviceConfig = {
        Program = "/Library/Application Support/org.nixos.kanata/kanata";
        ProgramArguments = [
          "/Library/Application Support/org.nixos.kanata/kanata"
          "--cfg"
          "${cfg.configFile}"
        ];
        Label = "org.nixos.kanata";
        ProcessType = "Interactive";
        KeepAlive = cfg.keepAlive;
        RunAtLoad = true;
      };
    };
  };
}