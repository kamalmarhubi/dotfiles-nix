function __fish_jj_grove_global_optspecs
    printf '%s\n' \
        R/repository= \
        ignore-working-copy \
        no-integrate-operation \
        ignore-immutable \
        at-operation= \
        at-op= \
        debug \
        color= \
        quiet \
        no-pager \
        config= \
        config-file= \
        h/help \
        V/version
end

function __fish_jj_grove_args_after_globals
    set -l words (commandline -xpc)
    set -e words[1]

    argparse -s (__fish_jj_grove_global_optspecs) -- $words 2>/dev/null
    or return 1

    if test (count $argv) -gt 0
        printf '%s\n' $argv
    end
    return 0
end

function __fish_jj_grove_args_after_grove
    set -l args (__fish_jj_grove_args_after_globals)
    or return 1

    test (count $args) -gt 0
    or return 1
    test "$args[1]" = grove
    or return 1

    set -e args[1]
    if test (count $args) -gt 0
        printf '%s\n' $args
    end
    return 0
end

function __fish_jj_grove_is_active
    __fish_jj_grove_args_after_grove >/dev/null
end

function __fish_jj_grove_needs_jj_subcommand
    set -l args (__fish_jj_grove_args_after_globals)
    or return 1

    test (count $args) -eq 0
end

function __fish_jj_grove_needs_subcommand
    set -l args (__fish_jj_grove_args_after_grove)
    or return 1

    test (count $args) -eq 0
end

function __fish_jj_grove_using_command
    set -l args (__fish_jj_grove_args_after_grove)
    or return 1

    test (count $args) -gt 0
    or return 1

    contains -- $args[1] $argv
end

function __fish_jj_grove_workspace_needs_subcommand
    set -l args (__fish_jj_grove_args_after_grove)
    or return 1

    test (count $args) -eq 1
    and test "$args[1]" = workspace
end

function __fish_jj_grove_using_workspace_command
    set -l args (__fish_jj_grove_args_after_grove)
    or return 1

    test (count $args) -gt 1
    or return 1
    test "$args[1]" = workspace
    or return 1

    contains -- $args[2] $argv
end

function __fish_jj_grove_using_mutating_command
    __fish_jj_grove_using_command new use rm graft sync restack push
    or __fish_jj_grove_using_workspace_command add
end

function __fish_jj_grove_revs_expected
    set -l args (__fish_jj_grove_args_after_grove)
    or return 1

    test (count $args) -gt 0
    or return 1
    contains -- $args[1] graft restack
    or return 1

    set -e args[1]
    argparse -s q/quiet n/dry-run push -- $args 2>/dev/null
    or return 1

    test (count $argv) -eq 0
end

function __fish_jj_grove_completion_global_args
    set -l words (commandline -xpc)
    set -e words[1]

    argparse -s (__fish_jj_grove_global_optspecs) -- $words 2>/dev/null
    or return

    for repository in $_flag_repository
        printf '%s\n' --repository "$repository"
    end
    for op in $_flag_at_operation $_flag_at_op
        printf '%s\n' --at-operation "$op"
    end
    for config in $_flag_config
        printf '%s\n' --config "$config"
    end
    for config_file in $_flag_config_file
        printf '%s\n' --config-file "$config_file"
    end
end

function __fish_jj_grove_complete_revs
    set -l fake_words jj
    set -a fake_words (__fish_jj_grove_completion_global_args)
    set -a fake_words log -r

    set -l command (string join -- ' ' (string escape -- $fake_words))
    set -l token (commandline -ct)
    if test -n "$token"
        set command "$command "(string escape -- "$token")
    else
        set command "$command "
    end

    complete -C "$command"
end

complete --keep-order --exclusive --command jj \
    --condition 'not __fish_jj_grove_is_active' \
    --arguments '(COMPLETE=fish jj -- (commandline --current-process --tokenize --cut-at-cursor) (commandline --current-token))'

complete -c jj -n '__fish_jj_grove_is_active' -f

complete -c jj -n '__fish_jj_grove_needs_jj_subcommand' -f -a grove \
    -d 'Manage groves and their workspaces'

complete -c jj -n '__fish_jj_grove_needs_subcommand' -f -a new \
    -d 'Create a grove'
complete -c jj -n '__fish_jj_grove_needs_subcommand' -f -a workspace \
    -d 'Manage grove workspaces'
complete -c jj -n '__fish_jj_grove_needs_subcommand' -f -a use \
    -d 'Bind the current workspace to a grove'
complete -c jj -n '__fish_jj_grove_needs_subcommand' -f -a list \
    -d 'List groves and workspaces'
complete -c jj -n '__fish_jj_grove_needs_subcommand' -f -a rm \
    -d 'Remove a grove'
complete -c jj -n '__fish_jj_grove_needs_subcommand' -f -a graft \
    -d 'Graduate work onto trunk'
complete -c jj -n '__fish_jj_grove_needs_subcommand' -f -a sync \
    -d 'Rebase grove roots onto trunk'
complete -c jj -n '__fish_jj_grove_needs_subcommand' -f -a restack \
    -d 'Rebase grove bookmarks onto trunk'
complete -c jj -n '__fish_jj_grove_needs_subcommand' -f -a push \
    -d 'Push grove bookmarks'

complete -c jj -n '__fish_jj_grove_workspace_needs_subcommand' -f -a add \
    -d 'Add a grove workspace'

complete -c jj -n '__fish_jj_grove_using_mutating_command' -s q -l quiet \
    -d 'Suppress narration'
complete -c jj -n '__fish_jj_grove_using_mutating_command' -s n -l dry-run \
    -d 'Print the plan; change nothing'

complete -c jj -n '__fish_jj_grove_using_command new' -l bare \
    -d 'Create the grove without adding a workspace'
complete -c jj -n '__fish_jj_grove_using_command new' -l root -r -f \
    -a '(__fish_complete_directories)' -d 'Workspace directory'

complete -c jj -n '__fish_jj_grove_using_workspace_command add' -l root -r -f \
    -a '(__fish_complete_directories)' -d 'Workspace directory'

complete -c jj -n '__fish_jj_grove_using_command rm' -l force \
    -d 'Skip guards and confirmation'

complete -c jj -n '__fish_jj_grove_using_command graft restack' -l push \
    -d 'Also push the bookmarks'

complete -c jj -n '__fish_jj_grove_using_command sync' -l no-fetch \
    -d 'Skip the git fetch'
complete -c jj -n '__fish_jj_grove_using_command sync' -l keep-merged \
    -d "Don't prune merged bookmarks"

complete -c jj -n '__fish_jj_grove_revs_expected' -f \
    -a '(__fish_jj_grove_complete_revs)'
