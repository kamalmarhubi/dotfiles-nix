function clone --description 'Clone into the configured repos root, optionally changing directory'
    argparse --name=clone cd h/help -- $argv
    or return

    if set -q _flag_help
        command clone --help
        printf '\nFish option: --cd changes directory after a successful clone.\n'
        return
    end

    if set -q _flag_cd
        set -l destination (CLONE_RESULT_FD=3 command clone $argv 3>&1 1>&2)
        or return
        cd -- "$destination"
    else
        command clone $argv
    end
end
