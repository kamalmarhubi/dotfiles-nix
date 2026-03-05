{
  pkgs,
  ...
}: {
  home.packages =
    (with pkgs.llm-agents; [
      chainlink
      claude-code
      claude-code-acp
      # mcporter
    ])
    ++ (with pkgs; [
      # zig and e2fsprogs for https://github.com/earendil-works/gondolin
      e2fsprogs
      zig
    ]);

  nixpkgs.allowUnfreePackages = with pkgs.llm-agents; [
    claude-code
  ];
}
