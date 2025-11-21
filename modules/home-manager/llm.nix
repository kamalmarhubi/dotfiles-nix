{
  pkgs,
  ...
}: {
  home.packages = with pkgs; [
    unstable.claude-code
    master.claude-code-acp
  ];

  nixpkgs.allowUnfreePackages = with pkgs; [
    unstable.claude-code
  ];
}
