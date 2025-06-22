{ config, lib, pkgs, ... }:

with lib;

let
  cfg = config.services.karabiner-driverkit-virtualhiddevice;
  karabinerDriverPackage = pkgs.karabiner-elements;
in

{
  options = {
    services.karabiner-driverkit-virtualhiddevice = {
      enable = mkEnableOption "Karabiner DriverKit VirtualHIDDevice driver";
    };
  };

  config = mkIf cfg.enable {
    # Install VirtualHIDDevice driver
    system.activationScripts.karabiner-driverkit-virtualhiddevice.text = ''
      echo "setting up Karabiner DriverKit VirtualHIDDevice driver..." >&2

      # Create dedicated application directory
      parentAppDir="/Applications/.Nix-Karabiner-DriverKit-VirtualHIDDevice"
      rm -rf "$parentAppDir"
      mkdir -p "$parentAppDir"

      # Copy VirtualHIDDevice Manager from karabiner package
      # Kernel extensions must reside inside of /Applications, they cannot be symlinks
      cp -r ${karabinerDriverPackage.driver}/Applications/.Karabiner-VirtualHIDDevice-Manager.app "$parentAppDir"
    '';

    launchd.daemons.start_karabiner_driverkit_virtualhiddevice = {
      script = ''
        exec "${karabinerDriverPackage.driver}/Applications/.Karabiner-VirtualHIDDevice-Manager.app/Contents/MacOS/Karabiner-VirtualHIDDevice-Manager" activate
      '';
      serviceConfig = {
        Label = "start_karabiner_driverkit_virtualhiddevice";
        RunAtLoad = true;
      };
    };
  };
}