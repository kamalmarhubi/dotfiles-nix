{
  pkgs,
  ...
}: {
  home.packages = with pkgs.llm-agents; [
    chainlink
    claude-code
    claude-code-acp
  ];

  nixpkgs.allowUnfreePackages = with pkgs.llm-agents; [
    claude-code
  ];
}
