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
    services.karabiner-driverkit-virtualhiddevice.enable = mkDefault true;

    environment.systemPackages = [ cfg.package ];

    # Run as system daemon to access HID devices (requires root permissions)
    launchd.daemons.kanata = {
      serviceConfig = {
        Program = "${lib.getExe cfg.package}";
        ProgramArguments = [
          "${lib.getExe cfg.package}"
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