---
timestamp: 2026-08-25T17:30:22Z
kind: code-review
target: files/jj/jj-stack
effort: high
focus: simplification, readability, modifiability, code smells
findings: 31
---

# Code review: files/jj/jj-stack

Whole-file review of all 3,370 lines, read-only — no commands run, no tests run. The
brief was simplification, readability, ease of modification, and code smells, so
findings are grouped by theme rather than strictly by severity; within each theme the
higher-impact items come first.

Every mechanical claim (call-site counts, dead code, duplicate literals) was verified by
grep against the file. Items resting on multi-hop reasoning about reachability are marked
**Plausible**; everything else is **Confirmed**.

Three correctness items surfaced while reading for structure. They lead, because they
change behavior rather than just readability.

---

## Correctness found while reading for structure

### 1. `--color=never` is per-call and missing from four `jj` invocations — `:1752`

**Confirmed.** The flag appears at 14 call sites. It is absent from `operation_id()`
(`:1752`), `workspace_targets()` (`:1681`), `boundary_commands()` (`:1094`) and
`verify_published_branch_tracking()` (`:1878`).

Under the default `ui.color = "auto"` the flag is redundant — `command()` uses
`capture_output=True`, so stdout is never a TTY and jj disables color on its own. That is
why extensive testing never exposed the gap. Set `ui.color = "always"` and
`operation_id()` returns an ANSI-wrapped operation id; every later `jj_inspect` passes it
as `--at-op=<escaped>`, so the run dies at `begin_inspection_phase()` before touching
anything.

Fix and simplification are the same edit: put `--color=never` (and arguably `--no-pager`)
into the `JJ` prefix built by `jj_command()`, then delete 14 duplicated flags.

### 2. `workspace_targets()` re-queried inside its own loop — `:1804`

**Confirmed.** `verify_rewritten_closure` calls `dict(workspace_targets())` inside the
per-workspace loop. That is one subprocess per workspace, and each iteration reads a
*different* snapshot.

With concurrent workspace activity the loop can accept workspace N against one snapshot
and reject workspace N+1 against a later one, reporting "workspace did not remain
attached to its planned change" for state that moved mid-loop. Hoist
`current = dict(workspace_targets())` above the loop. The repeated `change_id_at()` calls
at `:1805` and `:1813` deserve the same treatment — they re-derive a mapping the loop
could build once.

### 3. `graphql()` assumes the response is a dict — `:569`

**Confirmed.** `errors = response.get("errors")` runs with no type check, and
`AttributeError` is not in the `except (KeyError, TypeError)` tuple below it.

If `gh api graphql` prints `null` or a JSON array — proxy and edge error shapes do this —
`.get` raises `AttributeError` and the user sees a raw traceback instead of the intended
`jj stack: could not ...` message. Guard with `isinstance(response, dict)`, or widen the
caught tuple.

---

## Dead code and unreachable branches

### 4. `validate_selection` has no callers — `:248`

**Confirmed** by repo-wide grep: the definition is the only hit, tests included.
`validate_boundaries` is the real entry point. Worse than merely unused — the dead one
takes a raw revset while the live one takes a boundary list, so it reads like the
convenient wrapper and is an easy wrong turn for anyone adding a caller. Delete it.

### 5. `StackSnapshot.candidates` is write-only — `:478`

**Confirmed.** Written twice — positionally at `:1000`, by attribute at `:1009` — and
never read anywhere in the script or the tests. Drop the field and the re-discovery it
stores; otherwise the next reader assumes it means something and threads `discovered`
around to keep feeding it.

### 6. `required_titles=None` mode has no caller — `:309`

**Confirmed.** The single caller (`:2689`) always passes a set, so the "every revision
needs a title" mode is dead. The rule is then re-checked at `:2727` for auto-discovered
boundaries, which is the check that actually matters. Three states to reason about
(`None` / empty set / populated set) for a parameter with one live mode, and two places
enforcing one rule. Drop the parameter and leave `:2727` as the single authority.

### 7. `has_new_boundaries` can never be `True` in the integrated flow — `:1540`

**Plausible** — rests on reachability reasoning across two call sites rather than a grep.

`main` computes `has_new_boundaries = len(states) != len(revisions)`. `boundary_states`
skips exactly the heads that `has_topology_delta`'s first disjunct counts, so
`has_new_boundaries` implies `topology_changed` — which returns early at `:1546`. That
makes the `has_new_boundaries or ...` disjunct at `:1553` and the
`NORMAL_PUBLISH if has_new_boundaries` at `:1565` unreachable in production. Only unit
tests exercise them.

The cost is modifiability: anyone adding a fifth `ReconciliationKind` will reason about
branches that cannot fire. Derive both signals from one function, or drop the parameter,
so the invariant is stated once instead of emerging from two call sites that happen to
agree.

---

## Repetition that should be one helper

### 8. `gh_env` + `git_root` travel together through 14 signatures — `:549`

**Confirmed.** 14 functions declare `gh_env: dict[str, str]`, and nearly all pair it with
`git_root: str` — which is always `repo.git_root`. `graphql`, `fetch_stack`,
`discover_pull_requests`, `discover_merged_selectors`, `selector_stack`,
`fetch_pull_request`, `verify_unstacked`, `close_omitted_pull_request`,
`synchronize_pull_requests`, `inspect_existing_stack` all carry the pair.

A single `GitHub` context object (or passing the existing `Repository` plus `gh_env`)
removes one parameter from a dozen signatures and kills the possibility of a caller
pairing the wrong root with the wrong env. This is the largest single readability win in
the file.

### 9. `verify_final` takes 11 positional arguments and is called twice, identically — `:2491`

**Confirmed.** The 11-argument call is copy-pasted at `:3297` and `:3320`. `print_plan`
takes 10 (8 positional, 2 keyword-only) for a single call site. `inspect_existing_stack`
and `discover_publication` take 7 each.

Two identical 11-argument calls 23 lines apart is a standing invitation to update one and
not the other. Group the stable arguments (`repo`, `metadata`, `expected_drafts`,
`expected_first_base`, `base_oid`, `counts`, `gh_env`) into the context object from
finding 8 and the two calls collapse to something a reader can diff at a glance.

### 10. The git argv prefix is written out nine times — `:1152`

**Confirmed.** `["git", "--no-replace-objects", "-C", git_root, ...]` appears 9 times.
Separately, the exact argv `rev-list --parents -n 1 <oid>` is built 3 times — in
`git_linear_segment` (`:1188`), `git_last_linear_commits` (`:1229`) and
`git_commit_parent` (`:1248`).

A `git_read(git_root, *args)` helper plus a `parents_of(git_root, oid)` collapse both. As
it stands, adding a flag like `--no-optional-locks` to the read path means finding nine
sites and not missing one.

### 11. The mechanical-restack comparison is duplicated verbatim — `:1370`

**Confirmed.** The change-id-and-patch-id pair comparison, and its "contains remote-only
edits; the live suffix is not a mechanical GitHub restack" message, appear twice:
`:1370-1376` in `validate_existing_suffix` and `:1481-1487` in
`verify_mechanical_restack`. This is the security-relevant check in the file — the one
deciding whether a remote rewrite was mechanical. It should exist once, as
`assert_mechanical_pair(git_root, old_oid, new_oid)`.

### 12. Both interactive prompts duplicate the isatty/EOFError/y-N block — `:2192`

**Confirmed.** `authorize_omissions` (`:2192`) and `authorize_separate_stack` (`:2233`)
each carry the same ten lines: `sys.stdin.isatty()` check, `input()` in a `try`, `except
EOFError: answer = ""`, then `answer.strip().lower() not in {"y", "yes"}`. Extract
`confirm(question, *, noninteractive_error)`.

### 13. 42 repeats of `file=sys.stderr`, and two oid widths — `:2058`

**Confirmed.** 42 `print(..., file=sys.stderr)` calls. Oid truncation splits 39 uses of
`[:12]` against 8 of `[:8]`, so the same oid renders at two widths in adjacent lines.
`print_plan` is roughly 140 lines of mostly formatting plumbing.

A `note(*lines)` helper writing to stderr and a `short(oid)` helper remove the repeated
keyword argument and make the truncation width one decision. The keyword repetition is
also a live hazard — see finding 27 on the stdout protocol.

### 14. `open_suffix_root` exists but the logic is also inlined — `:493`

**Confirmed.** `open_suffix_root(stack, base_oid)` returns
`stack.merged[-1].head_oid if stack.merged else base_oid`. `validate_existing_suffix`
inlines the same expression at `:1316` with `None` as the fallback instead. Two
spellings, one differing in fallback only. Give the helper an explicit default parameter
and use it in both places, so the difference is visible at the call site rather than
buried in a re-implementation.

---

## Structure and modifiability

### 15. `main()` is 565 lines, and its dry-run branch duplicates the live one — `:2800`

**Confirmed.** ~30 locals, with `discovered`, `selection`, `revisions`, `heads`, `stack`,
`open_oids` and `planned_operation` each rebound several times. The dry-run branch
(`:2986-3040`) derives its command list independently of the live path.

Failure mode is drift: change how commands are built on one side and `--dry-run` prints a
plan the live run no longer executes — the one thing a dry run exists to prevent. Split
into `plan()`, `reconcile()`, `publish_refs()`, `publish_github()`, `verify()` over a
frozen context, so both paths render from one source.

### 16. `planned_operation` is reassigned eight times with an implicit protocol — `:2803`

**Confirmed.** The sequence "mutate the repo, then call `begin_inspection_phase()` and
rebind `planned_operation`" appears seven times, and `operation_id() != planned_operation`
is checked at four separate points. Nothing enforces the pairing.

Forget one `begin_inspection_phase()` after a mutation and every later `jj_inspect` reads
a stale operation — silently, because a stale-but-valid operation id still resolves. A
context manager (`with reinspect_after_mutation():`) or a small `Inspection` object that
owns the global makes the pairing impossible to omit. This also retires the
module-level `INSPECTION_OPERATION` global at `:77`.

### 17. The rebuild-kind set is spelled out at six decision sites — `:1906`

**Confirmed.** `TopologyKind.FULLY_OPEN_REBUILD` appears in six decisions beyond its
definition: `:1931` (`verify_unstackable`), `:1971` and `:1993` (`make_plan`), `:2033`
(`expected_first_open_base`), `:2175` and `:2302`, `:3014` (dry-run print), `:3249`
(unstack). Some pair it with `MERGED_PREFIX_REBUILD`, some with `NEW` — the groupings are
not identical, which is exactly what makes them hard to audit.

Adding a sixth `TopologyKind` means finding all six sets and getting each grouping right.
Name the predicates once — `TopologyKind.rebuilds_stack()`, `.needs_link_all()` — and the
call sites become readable assertions instead of set literals to cross-check.

### 18. `make_plan`'s nine-branch chain sets two variables at once — `:1944`

**Confirmed.** The if/elif chain assigns both `kind` and `link_heads`, and `link_heads`
is just `heads` in six of seven branches (`[]` in two, `heads[len(existing):]` for
APPEND). A new branch that forgets `link_heads` is an `UnboundLocalError` at runtime, not
a type error at edit time.

Decide `kind` in the chain, then derive `link_heads` from `(kind, heads, existing)`
underneath it. Seven assignments become one, and the derivation becomes a single
readable rule.

### 19. `StackSnapshot` is mutable only so one function can backfill a field — `:472`

**Confirmed.** Every other dataclass in the file is `frozen=True`. `StackSnapshot` is
not, purely so `inspect_existing_stack` can assign `.discovered` (and the dead
`.candidates`) after construction at `:1008-1009`.

In a script whose entire thesis is comparing snapshots before and after, a snapshot that
can be mutated in place undercuts the guarantee — `verify_stack_snapshot_unchanged` can be
handed a `before` that something already edited. Freeze it and use
`dataclasses.replace()`.

### 20. `frozen=True` on dataclasses holding mutable lists — `:1914`

**Confirmed.** `Plan` is frozen but declares `heads: list[str]`, `omitted:
list[PullRequest]`, `link_heads: list[str]`. `StackSnapshot.entries` is a `list` too.
Frozen prevents rebinding the attribute, not mutating the list, so the annotation
promises more immutability than it delivers. Use tuples, matching `Selection` and
`ClosureSnapshot` which already do.

### 21. `verify_unstacked`'s parameter is named `old` but receives the new snapshot — `:2326`

**Confirmed.** At `:3266` the call passes `stack` — the post-reconciliation snapshot —
while `old_stack` is also in scope. It is not a bug:
`verify_stack_snapshot_unchanged(old_stack, stack)` at `:3197` already proved the two
equal. But a reader checking this line has to reconstruct that proof to convince
themselves, and a future edit that weakens the equality check breaks this silently.
Either pass `old_stack` or rename the parameter to what it is.

### 22. `command()`'s parameter shadows the `input` builtin — `:51`

**Confirmed.** `command(..., input: str | None = None)` shadows the builtin inside that
function, and the same file uses the builtin `input()` for prompts at `:2217` and `:2239`.
It works today because the scopes are separate. Adding any prompt or debug read inside
`command()` would silently call the `str` parameter instead. `stdin_text` costs nothing
and removes the trap.

---

## Efficiency

### 23. `revision_details` spawns six `jj log` processes per revision — `:308`

**Confirmed.** Identity, local bookmarks, remote bookmarks, conflicted bookmarks,
description and push-template are six separate calls. A 10-commit range costs 60
subprocesses, and `discover_publication` may run it up to three times per invocation.

One templated call per revision — fields joined by `\x1f`, rows by `\x1e` — collapses
this, and makes the near-duplicate `change_id_at()` (`:867`) redundant.

### 24. `discover_merged_selectors` queries one OID at a time — `:597`

**Confirmed.** A GraphQL round trip per selected OID inside a `for` loop. Meanwhile
`discover_pull_requests` (`:773`) batches every bookmark into one aliased query. The
batching technique is already in the file, applied to the sibling problem, 170 lines
away. Apply it here and n selectors cost one request.

### 25. Per-commit `git rev-list` inside the linearity loops — `:1167`

**Confirmed.** `git_linear_segment` and `git_last_linear_commits` (`:1212`) each run one
`git rev-list --parents -n 1` per commit, and both run for the tracked *and* live segment
of every active PR. Process count is O(PRs x commits) on every invocation — a 5-PR stack
of 4 commits each pays ~40 extra git processes.

A single `git rev-list --parents --reverse base..tip` returns every commit with its
parents at once; the linearity check becomes a pure loop over parsed output with no I/O.

### 26. `selector_stack`'s pairwise ancestry check runs two jj queries per pair — `:701`

**Confirmed.** The nested loop at `:725-739` calls `revision_ids` twice per selector
pair, so n merged selectors cost up to n(n-1) subprocesses to answer a question one
revset over the union could answer once.

### 27. `validate_boundaries` runs a subprocess per commit for the parent chain — `:237`

**Confirmed.** Six-plus full revset queries, then `revision_ids(f"parents({commit_id})")`
once per commit in the range. The whole parent-chain check is one templated
`jj log -r fork..tip` returning each commit with its parents. Related smell:
`revision_ids()` is used as a boolean predicate roughly 15 times across the file
(`if revision_ids(f"{x} & hidden()")`), each one a process spawn, each one reading as a
cheap set membership test.

### 28. `verify_detached_omissions` re-reads all remote refs per closed PR — `:2426`

**Confirmed.** Called once for the whole evidence tuple at `:3322`, then again with a
1-tuple inside the close loop at `:3325`. Each call does a full `remote_refs` (a network
`ls-remote`) plus a `fetch_pull_request` GraphQL round trip. Closing three PRs costs four
`ls-remote` calls and four-plus GraphQL requests where two would do.

---

## Output and interface consistency

### 29. Title escaping is inconsistent, and the helper is used once — `:2044`

**Confirmed.** `terminal_text()` (json-encode, strip the surrounding quotes) is called at
exactly one site, `:2125`. At `:988` a commit title goes to the terminal through
`json.dumps(revision.title)` directly, keeping the quotes. So the same class of
untrusted, potentially control-character-bearing text renders two different ways, and the
helper that exists to normalize it is bypassed in one of its two applicable places.
Bookmark names (`head`) are printed raw throughout. Pick one rendering for
externally-supplied text and route all three through it.

### 30. The stdout/stderr protocol is load-bearing and undocumented — `:3348`

**Confirmed.** Exactly one `print` in the file omits `file=sys.stderr`: the
`Published stack {n}: {url}` line at `:3348`. Everything else — the whole plan, the mode
banner, even the `Omitted PR #n detached` results at `:3354` — goes to stderr. That is a
deliberate and useful contract: stdout carries the machine-readable result.

Nothing says so. There is no comment, and given 42 hand-written `file=sys.stderr`
arguments, the likeliest edit is someone "fixing the inconsistency" in either direction
and breaking anything parsing stdout. The `note()` helper from finding 13 plus one comment
on `:3348` makes the contract self-enforcing.

### 31. Four raw `subprocess.run` calls bypass `command()` — `:2349`

**Confirmed.** `command()` is the single wrapper for capture, error text, and the `Error`
message format. Four calls sidestep it: `:2349` (`run_stack_link`), `:3201` (boundary
commands), `:3254` (`gh stack unstack`), `:3267` (`gh pr edit`).

For the three `gh` calls this looks deliberate — output streams to the user's terminal.
The consequence is that their failures raise messages with no captured detail
("could not unstack GitHub stack; the stack may be partially changed") while every
`command()` failure appends the underlying stderr. A `command(..., stream=True)` variant
would keep the streaming behavior and still let failures carry detail.

---

## Checked and believed correct

Verified while reading; not flagged:

- The `--force-with-lease` construction in `atomic_push_command` and its lease sources.
- `open_oids` re-derivation after `REMOTE_ADOPT` / `LOCAL_AUTHORITY` — change-id-keyed,
  so `publication_identity` still matches.
- `expand_open_suffix`'s single pass: expandable oids are always inside `fork..tip`, so
  the range cannot grow on a second iteration.
- `str(plan.stack_number)` at `:3252` — every reaching branch has a non-`None` number,
  though the invariant is unstated and an `assert` would document it.
- `git_patch_id` returning `None` for empty diffs — always paired with a change-id
  comparison.
- `evidence_by_number[number]` at `:3326` — an unguarded lookup safe only because omitted
  PRs imply `topology_delta`, which implies `existing_evidence` was computed. Three hops,
  all currently holding; the failure mode if one breaks is a `KeyError` traceback rather
  than a clean error.
- `time.sleep(1)` at `:3310` before the second `verify_final`: a fixed delay is a guess
  about GitHub's restack latency, and the first `verify_final` result is assigned and
  discarded. A bounded poll on `remote_refs` until stable for N reads would state the
  intent the sleep only approximates. Not counted as a finding since the current behavior
  is a deliberate, tested tradeoff.
