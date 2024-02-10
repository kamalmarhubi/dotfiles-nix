{
  security.pam.enableSudoTouchIdAuth = true;
  # Default to requiring authentication on every sudo invocation...
  environment.etc."sudoers.d/10-timestamp_timeout".text = ''
    Defaults        timestamp_timeout=0
  '';

  # ... but unset that during activation since it runs a lot of sudo stuff
  system.activationScripts.preActivation.text = let
    sudoersTmp = "/etc/sudoers.d/99-timestamp_timeout_tmp";
    in ''
    sudo cat > ${sudoersTmp} <<EOF
    Defaults        timestamp_timeout=-1  # Indefinite
    EOF
    sudo --validate
    trap 'sudo rm -f "${sudoersTmp}"; sudo --remove-timestamp' EXIT
  '';
}
