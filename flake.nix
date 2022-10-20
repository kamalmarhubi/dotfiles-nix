{
  description = "Kamal pretends to use nix?";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { nixpkgs, home-manager, ... }: {
    homeConfigurations = {
      "kamal@kx7" = let
        system = "x86_64-linux";
        pkgs = nixpkgs.legacyPackages.${system};
        homeDirectory = "/home/kamal";
        repoCloneDirectory = "${homeDirectory}/.local/share/dotfiles-nix";
        repoUrl = "https://github.com/kamalmarhubi/dotfiles-nix.git";
      in
      home-manager.lib.homeManagerConfiguration {
        inherit pkgs;

        # Specify your home configuration modules here, for example,
        # the path to your home.nix.
        modules = [{
          programs = {
            home-manager.enable = true;
            fish.enable = true;
            neovim = {
              enable = true;
              plugins = with pkgs.vimPlugins; [
              ];
            };
          };

          home = {
            username = "kamal";
            inherit homeDirectory;
            stateVersion = "22.11";

            sessionVariables = {
              EDITOR = "nvim";
            };

            activation.checkGitRepoExists = let
              checkGitRepoExists = pkgs.writeShellApplication {
                name = "check-git-repo-exists";
                runtimeInputs = [ pkgs.git ];
                text = ''
                  die() {
                    echo "$@" >&2
                    exit 1
                  }
                  target_dir="${repoCloneDirectory}"
                  git_origin="${repoUrl}"

                  $VERBOSE_ECHO "Checking if $target_dir exists"
                  test ! -e "$target_dir" && $VERBOSE_ECHO "$target_dir does not exist" && exit 0

                  $VERBOSE_ECHO "Checking if $target_dir is a directory"
                  test -d "$target_dir" || die "$target_dir exists but is not a directory"
                  $VERBOSE_ECHO "$target_dir is a directory"

                  $VERBOSE_ECHO "Checking if $target_dir is a git repo"
                  test "$(git -C "$target_dir" rev-parse --show-toplevel 2>/dev/null)" = "$target_dir" \
                    || die "$target_dir is not a git repo"
                  $VERBOSE_ECHO "$target_dir is a git repo"

                  $VERBOSE_ECHO "Checking if $target_dir's origin is set correctly"
                  test "$(git -C "$target_dir" config --local --get remote.origin.url 2>/dev/null)" = "$git_origin" \
                    || die "$target_dir's remote is not set to $git_origin"
                  $VERBOSE_ECHO "$target_dir looks good!"
                '';
              };
            in
            home-manager.lib.hm.dag.entryBefore ["writeBoundary"] ''
              ${checkGitRepoExists}/bin/check-git-repo-exists
            '';
            activation.cloneGitRepoIfNeeded = let
              cloneGitRepoIfNeeded = pkgs.writeShellApplication {
                name = "clone-git-repo-if-needed";
                runtimeInputs = [ pkgs.git ];
                text = ''
                if [ -e "${repoCloneDirectory}" ]; then
                  $VERBOSE_ECHO "${repoCloneDirectory} exists; skipping clone"
                  exit 0
                fi
                $DRY_RUN_CMD git clone "${repoUrl}" "${repoCloneDirectory}"
                '';
              };
            in
            home-manager.lib.hm.dag.entryAfter ["writeBoundary"] ''
              ${cloneGitRepoIfNeeded}/bin/clone-git-repo-if-needed
            '';
            # THEN:
            #   Use mkOutOfStoreSymlink to link ~/.config/nvim to $BLAH/config/nvim
            #   etc
          };
        }];
      };
    };
  };
}
