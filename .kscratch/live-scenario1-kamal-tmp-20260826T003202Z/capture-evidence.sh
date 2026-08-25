#!/bin/sh
set -eu
label=$1; root=$2; repo=$3; d=$4; out="$root/evidence/$label"; mkdir -p "$out"
date -u +%FT%TZ > "$out/time"
for q in 'stacks?per_page=100' 'stacks/4'; do n=$(printf %s "$q"|tr '?&=/' '____'); gh api --include --paginate "repos/$repo/$q" > "$out/rest-$n"; done
for p in 1 2 3; do
 gh api --include --paginate "repos/$repo/stacks?pull_request=$p&per_page=100" > "$out/rest-stacks-pr$p"
 gh api --include --paginate -H 'Accept: application/vnd.github+json' "repos/$repo/issues/$p/timeline?per_page=100" > "$out/timeline-pr$p"
 gh api "repos/$repo/pulls/$p" > "$out/pull-pr$p.json"
 gh api graphql --paginate -f owner=${repo%/*} -f name=${repo#*/} -F number=$p -f query='query($owner:String!,$name:String!,$number:Int!,$endCursor:String){repository(owner:$owner,name:$name){pullRequest(number:$number){id number title body isDraft state baseRefName baseRefOid headRefName headRefOid timelineItems(first:100,after:$endCursor,itemTypes:[BASE_REF_CHANGED_EVENT]){nodes{... on BaseRefChangedEvent{previousRefName currentRefName createdAt actor{login}}}pageInfo{hasNextPage endCursor}}}}}' > "$out/graphql-pr$p.json"
done
(cd "$d"; git fetch origin '+refs/heads/*:refs/remotes/origin/*' >/dev/null 2>&1; git show-ref; echo TOPOLOGY; git log --all --graph --decorate --oneline; echo RAW; for r in origin/main origin/scenario1-a origin/scenario1-b origin/scenario1-c; do echo ===$r; git cat-file commit "$r"; done; echo PATCHIDS; for r in origin/scenario1-a origin/scenario1-b origin/scenario1-c; do git show "$r" --pretty=format: | git patch-id --stable; done; echo JJ; jj op log --no-graph -T 'id ++ " " ++ description ++ "\n"'; jj status; jj bookmark list --all-remotes; jj workspace list) > "$out/local.txt" 2>&1
(cd "$out"; shasum -a 256 * > MANIFEST.sha256)
