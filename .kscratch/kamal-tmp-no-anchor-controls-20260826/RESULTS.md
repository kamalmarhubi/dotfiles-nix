# Fresh no-anchor false-positive controls

Repository: `wavemm/kamal-tmp-no-anchor-controls-20260826` (private, retained)

Source SHA-256: `5e3c18debf997a45510ff099d05ab9ba41d317c8a40f202a2f205a99f80fd860` (immutable copy `jj-stack-source`). The working-tree source was not edited.

## Results

1. **Never-stacked ordinary chain:** PRs #1 (`ordinary-a`→`main`) and #2 (`ordinary-b`→`ordinary-a`) are a real OID ancestry/base chain, but GraphQL `stack` is null both before and after. Thus base/name/ancestry can mimic a stack while explicit membership says none.
2. **Stack then unstack, first base manually changed:** stack #6 held PRs #3–#5 before unstack. It returns 404 after `gh stack unstack 6`; all PR stack fields become null. #3 was then changed from `main` to `ordinary-a`, while #4/#5 retain historical-looking `c2-a`/`c2-b` bases and commit ancestry. A chain-history recovery can choose incompatible roots or treat a dissolved stack as current; refusal is required absent explicit membership/OID anchor.
3. **Multiple base changes:** stack #10 held #7–#9, then was dissolved. Open PR #7 was changed `main→ordinary-a→main→ordinary-a`. Ordered GraphQL `BaseRefChangedEvent` records include IDs, actors and timestamps, but contain only previous/current ref names; no stack ID or membership. REST timeline likewise emits base-reference changes, not stack membership.
4. **Surviving different stack:** stack #14 (#11–#13) survives. Its top `c4-c` is name-shaped like the candidate suffix, but exact head/base OIDs belong to an independent lineage from `main`; `git merge-base --is-ancestor(candidate-c2, c4-c)` is false. Explicit stack ID `PRS_kwDOUEXIf84ACYHo`, PR node identities, and ancestry reject it mechanically.
5. **Two surviving candidates:** stacks #18 (#15–#17, `c5a-*`) and #22 (#19–#21, `c5b-*`) survive simultaneously. Names/base histories alone provide two equally shaped three-PR suffixes. Their explicit stack IDs and PR node identities differ, and neither top OID is ancestor of the other. Without those signals (or a candidate OID ancestry match), recovery is ambiguous and must refuse rather than relink.

## Evidence semantics

Mechanically proven assertions are in `assertions.json`: exact OIDs, explicit GraphQL stack IDs/nulls, and `git merge-base --is-ancestor` booleans. Heuristic conclusions are labeled above (name/base-chain resemblance and what a historical heuristic might infer); they are not treated as proof.

`evidence/{before-unstack,immediate-after,delayed-after}` contains refs/JJ topology, REST stack list pagination (`per_page=2`, pages 1/2 and full page), state filters, stack GETs, PR topology, REST timeline pagination/pages/types, and GraphQL PR/stack/base-event pagination with event pageInfo. Immediate and delayed captures establish eventual state. No relinking was performed.
