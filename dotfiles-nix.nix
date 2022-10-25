{ config, pkgs, lib, ... }:
  let
    targetDir = "${config.home.homeDirectory}/.local/share/dotfiles-nix";
    repoUrl = "https://github.com/kamalmarhubi/dotfiles-nix.git";
    checkGitRepoOriginIfPresent = pkgs.writeShellApplication {
      name = "check-git-repo-origin-if-present";
      runtimeInputs = [ pkgs.git ];
      text = ''
        die() {
          echo "$@" >&2
          exit 1
        }
        target_dir="${targetDir}"
        git_origin="${repoUrl}"

        $VERBOSE_ECHO "Checking if $target_dir exists"
        test ! -e "$target_dir" && $VERBOSE_ECHO "$target_dir does not exist" && exit 0

        $VERBOSE_ECHO "Checking if $target_dir is a directory"
        test -d "$target_dir" || die "$target_dir exists but is not a directory"
        $VERBOSE_ECHO "$target_dir is a directory"

        $VERBOSE_ECHO "Checking if $target_dir is a git repo"
        test "$(git -C "$target_dir" rev-parse --show-toplevel 2>/dev/null)" = "$target_dir" \
        || die "$target_dir exists but is not a git repo"
        $VERBOSE_ECHO "$target_dir is a git repo"

        $VERBOSE_ECHO "Checking if $target_dir's origin is set correctly"
        origin="$(git -C "$target_dir" config --local --get remote.origin.url 2>/dev/null)"
        test "$origin" = "$git_origin" \
        || die "$target_dir's remote is not set to $git_origin"
        $VERBOSE_ECHO "$target_dir looks good!"
        '';
      };
    cloneGitRepoIfNeeded = pkgs.writeShellApplication {
      name = "clone-git-repo-if-needed";
      runtimeInputs = [ pkgs.git ];
      text = ''
        target_dir="${targetDir}"
        git_origin="${repoUrl}"
        if [ -e "$target_dir" ]; then
          $VERBOSE_ECHO "$target_dir exists; skipping clone"
            exit 0
        fi
        $DRY_RUN_CMD git clone "$git_origin" "$target_dir"
      '';
      };
  in {
  home.activation = {
      checkGitRepoOriginIfPresent = lib.hm.dag.entryBefore ["writeBoundary"] ''
        ${checkGitRepoOriginIfPresent}/bin/check-git-repo-origin-if-present
      '';
      cloneGitRepoIfNeeded = lib.hm.dag.entryAfter ["writeBoundary"] ''
        ${cloneGitRepoIfNeeded}/bin/clone-git-repo-if-needed
      '';
    };
    xdg.configFile."nix/nix.conf".text = ''
      experimental-features = nix-command flakes
    '';
    xdg.configFile."nixpkgs".source = config.lib.file.mkOutOfStoreSymlink "${config.home.homeDirectory}/.local/share/dotfiles-nix";
}
