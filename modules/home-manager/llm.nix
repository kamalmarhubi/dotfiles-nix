{
  config,
  pkgs,
  ...
}: {
  home.packages =
    (with pkgs; [
      unstable.codex
      # zig and e2fsprogs for https://github.com/earendil-works/gondolin
      e2fsprogs
      zig
      brewCasks.voiceink
    ])
    ++ (with pkgs.llm-agents; [
      amp
      chainlink
      claude-code
      claude-agent-acp
      mcporter
      opencode
      pi
    ]);

  nixpkgs.allowUnfreePackages = with pkgs.llm-agents; [
    claude-code
  ];

  xdg.configFile."amp/settings.json".source = config.lib.file.mkOutOfStoreSymlink "${config.xdg.configHome}/home-manager/files/amp/settings.json";
}
