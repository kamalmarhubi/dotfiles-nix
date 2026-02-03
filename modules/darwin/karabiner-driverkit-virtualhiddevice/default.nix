{ config, lib, pkgs, ... }:

with lib;

let
  cfg = config.services.karabiner-driverkit-virtualhiddevice;

  parentAppDir = "/Applications/.Nix-Karabiner-DriverKit";
in

{
  options.services.karabiner-driverkit-virtualhiddevice = {
    enable = mkEnableOption "Karabiner DriverKit Virtual HID Device";
    package = mkPackageOption pkgs "karabiner-dk" { };
  };

  config = mkIf cfg.enable {
    environment.systemPackages = [ cfg.package ];

    system.activationScripts.preActivation.text = ''
      rm -rf ${parentAppDir}
      mkdir -p ${parentAppDir}
      # System extensions must reside inside of /Applications, they cannot be symlinks
      # Use ditto to avoid resource fork files that break code signatures
      ditto --norsrc ${cfg.package}/Applications/.Karabiner-VirtualHIDDevice-Manager.app ${parentAppDir}/.Karabiner-VirtualHIDDevice-Manager.app
    '';

    system.activationScripts.postActivation.text = ''
      echo "attempt to activate karabiner virtual HID device system extension" >&2
      launchctl unload /Library/LaunchDaemons/org.nixos.karabiner_driverkit_activation.plist 2>/dev/null || true
      launchctl load -w /Library/LaunchDaemons/org.nixos.karabiner_driverkit_activation.plist
    '';

    # One-shot activation service - activates system extension then exits
    launchd.daemons.karabiner_driverkit_activation = {
      script = ''
        ${parentAppDir}/.Karabiner-VirtualHIDDevice-Manager.app/Contents/MacOS/Karabiner-VirtualHIDDevice-Manager activate
      '';
      serviceConfig.Label = "org.nixos.karabiner_driverkit_activation";
      serviceConfig.RunAtLoad = true;
      serviceConfig.KeepAlive = false;
    };

    # The VirtualHIDDevice-Daemon needs to run to provide the virtual HID device interface
    launchd.daemons.karabiner_driverkit_daemon = {
      script = ''
        exec "${cfg.package}/Library/Application Support/org.pqrs/Karabiner-DriverKit-VirtualHIDDevice/Applications/Karabiner-VirtualHIDDevice-Daemon.app/Contents/MacOS/Karabiner-VirtualHIDDevice-Daemon"
      '';
      serviceConfig = {
        ProcessType = "Interactive";
        Label = "org.nixos.karabiner_driverkit_daemon";
        KeepAlive = true;
        RunAtLoad = true;
      };
    };
  };
}