{
  pkgs,
  ...
}: {
  home.packages = with pkgs; [
    unstable.claude-code
  ];

  nixpkgs.allowUnfreePackages = with pkgs; [
    unstable.claude-code
  ];
}
