---
timestamp: 2026-08-24T20:12:42Z
kind: code-review
target: files/jj/jj-stack (and associated tests)
effort: high
---

# Code review: files/jj/jj-stack

High-effort review of `files/jj/jj-stack` and its tests. 10 correctness findings survived adversarial verification (7 confirmed by independent verifiers, 3 plausible), ranked most-severe first.

## Findings

### 1. Forbidden-ref check skipped for existing local bookmarks — `files/jj/jj-stack:908`

resolve_heads applies the `forbidden` ref-collision check only to generated bookmarks, so a local/remote bookmark named after the trunk or any existing unrelated remote branch is accepted as a push head and force-pushed.

Failure scenario: A selected boundary revision carries a single local bookmark `main` (== repo.base) or a colleague's existing branch name with no open PR. The `len(revision.local_bookmarks) == 1` branch (line 908) never consults `forbidden` (checked only in the generated-bookmark else-branch, lines 922-927). main() then builds RefUpdate(head, current_live.get(head), commit) so `git push --atomic --force-with-lease=refs/heads/main:<live base oid>` matches by construction and moves trunk (or the colleague's branch) to a stack commit. The 'base advanced concurrently' guard at line 2896 fires only AFTER trunk was clobbered; for non-base branches nothing fires at all.

### 2. PR-reuse guard is dead in the integrated flow — `files/jj/jj-stack:2799`

The 'reusing an existing PR for a different jj change is not supported' guard is dead in the integrated flow: planning always passes require_matching_change=False (lines 2560-2561, 2573-2574) and execution disables it exactly for LOCAL_AUTHORITY — the classification a moved bookmark produces.

Failure scenario: A local bookmark that is the head of open PR #12 is moved onto an unrelated jj change. Planning binds the revision to PR #12 with the guard off; local != live/tracked classifies as LOCAL_AUTHORITY (classify_reconciliation line 1507), so the execution-phase discover_publication also passes False. The tool rebases, force-pushes over PR #12's branch (lease matches its live OID), and synchronize_pull_requests overwrites its title/body — the exact outcome resolve_heads lines 897-906 exist to forbid. test_existing_pr_cannot_move_to_a_different_change only unit-tests resolve_heads with the default True, giving false coverage.

### 3. REMOTE_ADOPT spuriously fails when @ sits on the old stack — `files/jj/jj-stack:2761`

REMOTE_ADOPT's post-fetch check treats any still-visible old local commit as a failed adoption, but jj does not rebase/abandon old commits that have a working-copy descendant, so the mode spuriously fails whenever @ sits on the stack.

Failure scenario: GitHub mechanically restacks the stack (local == tracked != live) while the user's working-copy commit @ — or any workspace/extra bookmark — rests on the old stack tip, the normal state for a stack being actively worked on. `jj git fetch` moves the bookmarks but the old chain stays visible (jj only abandons unpinned commits and never auto-rebases descendants), so `revision_ids(f"{state.local_oid} & visible()")` is non-empty and main() aborts with 'jj did not exact-adopt GitHub revision ... for <branch>' even though the fetch behaved exactly as planned, making REMOTE_ADOPT unusable in the common case.

### 4. verify_live_suffix anchors at trunk tip, not merged boundary — `files/jj/jj-stack:1407`

verify_live_suffix anchors the open suffix at the current trunk tip (previous = base_oid) instead of the merged-prefix boundary, so the stale-merged-boundary refresh flow (needs_boundary_refresh -> MERGED_PREFIX_REBUILD) always aborts before executing, and a no-op rerun after trunk advances hard-fails with a misleading error.

Failure scenario: Stack = merged PR + open PR based on the merged boundary branch; main then advances. heads == existing so topology_delta is False and main() calls verify_reconciliation_suffix at line 2624; every branch funnels into verify_live_suffix, which fails 'PR #N has an unexpected live base; GitHub restack is incoherent' (line 1414) or git_linear_segment 'does not descend from expected base' (line 1192) because the first open PR's base_oid is the old trunk position, not base_oid. validate_existing_suffix (line 1307) shows the correct anchor (stack.merged[-1].head_oid) but only runs when topology_delta is True. The refresh plan asserted by test_stale_merged_boundary_rebuilds_before_refreshing_base is unreachable end-to-end; the test only unit-tests make_plan.

### 5. import_live_base checks fetch against the pre-fetch op — `files/jj/jj-stack:1073`

import_live_base validates its fetch with jj_commit_exists() still pinned via --at-op to the pre-fetch operation, so a successful base import raises a spurious 'live base changed while importing' error mid-publication.

Failure scenario: plan.boundary is set and the live base commit is not yet in jj (the common merged-boundary case). import_live_base runs `jj git fetch` (line 1071, creating a new operation), then re-checks jj_commit_exists(expected_oid) — but jj_inspect still queries `--at-op=<pre-fetch op>` (INSPECTION_OPERATION is only refreshed by main() at line 2909, after import_live_base returns). The freshly fetched commit is not in the old operation's index, the check fails, and the run aborts with 'live base ... changed while importing it into jj; replan and retry' — after the atomic branch push already landed (line 2878) but before the boundary advance.

### 6. Execution rediscovery can add unapproved PR boundaries — `files/jj/jj-stack:2444`

Execution-time rediscovery can silently add PR boundaries the user never approved: only the stack snapshot and plan.omitted are compared against the confirmed plan, never the heads list itself.

Failure scenario: Between the interactive confirmation (authorize_omissions, line 2670) and execution, someone opens an unstacked PR whose head matches an intermediate revision's bookmark or generated name. The second discover_publication (line 2790) auto-promotes that revision to a boundary (lines 2437-2445). verify_stack_snapshot_unchanged (line 2807) passes because the new PR is not a stack entry, and the omitted comparison (lines 2809-2812) is unaffected, so the tool pushes an extra branch, links an extra PR into the stack, and rewrites its metadata — actions absent from the plan the user confirmed. verify_final validates against the NEW heads (line 2337), so nothing catches it.

### 7. No-PR head lease from fresh read clobbers concurrent branch — `files/jj/jj-stack:2838`

For heads with no discovered PR, the force-with-lease value is taken from a fresh remote_refs read at line 2830 rather than the value the plan was validated against, so a branch created concurrently in that window is silently overwritten.

Failure scenario: A no-PR head (e.g. generated bookmark) passes the forbidden-set check against the ls-remote at line 2796; a colleague then pushes a branch with that name before the re-read at line 2830. RefUpdate gets old_oid = the colleague's OID, verify_live_boundary_refs_unchanged doesn't cover it (boundary_states line 1535 skips heads without PRs), and expected_before_publication (line 2869) compares against the same fresh read — the atomic push's lease matches trivially and the colleague's branch is silently clobbered. Every other ref class gets a 'replan and retry' guard; this one gets none.

### 8. Omission flow unreachable while a bookmark points off-stack — `files/jj/jj-stack:2385`

expand_open_suffix unconditionally unions the local-bookmark target of every unselected stack PR, making the documented omission flow (--detach-omitted / --close-omitted) unreachable while such a bookmark points off-stack.

Failure scenario: The user wants to drop PR #N and has moved its change out of the stack (rebased onto main as a sibling), but the local bookmark still exists at the relocated commit. expand_open_suffix injects local_bookmark_oid(pr.head) into the revset; validate_boundaries then fails 'requested history must have exactly one selected tip' (line 127) or the linear-chain check — a tip the user never selected — so the omission-authorization flow can never be reached until the user manually deletes the bookmark, and the error message gives no hint of that remedy.

### 9. Merged-only selectors skip expansion; active PRs detached — `files/jj/jj-stack:2376`

expand_open_suffix's early return on empty stack.discovered skips expansion when the stack was identified purely via merged selectors, so the stack's remaining active PRs are proposed for detachment instead of being pulled into the selection.

Failure scenario: Revset selects a merged-selector commit plus only new commits (no selected boundary has an open PR). inspect_existing_stack line 1013/1021 sets stack.discovered = selected_prs = {}, so `not discovered.stack.discovered` returns early even though stack.active is non-empty and the active PRs have local bookmarks in range. The plan then omits those PRs and prompts 'Detach #C from the stack and continue?' — the exact accidental-detach outcome expansion exists to prevent; a user who confirms detaches live stack PRs unintentionally.

### 10. Bookmark moved before push; failure claims no local change — `files/jj/jj-stack:1109`

boundary_commands moves the local bookmark before the remote push and interpolates the branch name unquoted into jj `exact:` patterns; a push failure then reports 'stack was not changed' despite the persisted local mutation.

Failure scenario: During the boundary advance (main lines 2911-2915), `jj bookmark set --allow-backwards -r <base_oid> <boundary>` succeeds, then the `jj git push --bookmark exact:<boundary>` fails (network/permissions, or a pattern-parse error: the name is GitHub's headRefName, which may contain characters jj's revset string-pattern grammar rejects or misparses — line 1566 quotes with json.dumps, lines 1103/1116 do not). The raised error 'could not advance the merged boundary; stack was not changed' is false locally: the bookmark was moved with no prior-position record and no rollback, and the failure lands after the atomic push already ran.

## Verified but cut (below the 10-finding cap)

- The LOCAL_AUTHORITY `ref_plan` override at `files/jj/jj-stack:2834` is a verbatim, deletable duplicate of what `make_ref_publication_plan` already returned at line 2833.
- `discover_publication` calls `discover_pull_requests` twice per pass with identical arguments (lines 2431 and 951), and `discover_merged_selectors` does one GraphQL round-trip per OID where the aliasing pattern at line 773 would batch them.
- The multi-commit metadata-authority policy is implemented independently in `print_plan` (lines 1976-1985) and `main` (lines 2814-2821), so the printed plan and executed mutation can drift.
- `git_commit_change_id` (line 1167) scans the commit message body as well as headers for `change-id `, so a message line starting with that prefix spuriously fails restack verification.

## Test gaps

- `test_existing_pr_cannot_move_to_a_different_change` passes while the guard it covers is disabled at every real call site (finding 2).
- `test_stale_merged_boundary_rebuilds_before_refreshing_base` asserts a plan that is unreachable end-to-end (finding 4); it only unit-tests make_plan.
