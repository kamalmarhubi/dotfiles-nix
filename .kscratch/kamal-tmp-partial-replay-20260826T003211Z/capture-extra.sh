#!/bin/sh
set -u
ROOT=$1 LABEL=$2 REPO=$3 GHREPO=$4
OUT="$ROOT/extra/$LABEL"; mkdir -p "$OUT"
date -u +%Y-%m-%dT%H:%M:%SZ > "$OUT/captured-at.txt"
for EP in "stacks?per_page=100" "stacks?per_page=100&state=open" "stacks?per_page=100&status=open"; do
 N=$(printf %s "$EP" | tr '?&=' '___')
 gh api --paginate --include "repos/$GHREPO/$EP" >"$OUT/rest-$N.stdout" 2>"$OUT/rest-$N.stderr"; echo $? >"$OUT/rest-$N.exit"
done
# Capture known old stack directly even after deletion.
gh api --include "repos/$GHREPO/stacks/5" >"$OUT/rest-stack-old.stdout" 2>"$OUT/rest-stack-old.stderr"; echo $? >"$OUT/rest-stack-old.exit"
gh api --paginate "repos/$GHREPO/pulls?state=all&per_page=100" >"$OUT/rest-pulls.json" 2>"$OUT/rest-pulls.stderr"; echo $? >"$OUT/rest-pulls.exit"
for N in $(gh pr list --repo "$GHREPO" --state all --limit 100 --json number --jq '.[].number'); do
 gh api --paginate "repos/$GHREPO/issues/$N/timeline?per_page=100" >"$OUT/timeline-$N.json" 2>"$OUT/timeline-$N.stderr"; echo $? >"$OUT/timeline-$N.exit"
 gh api graphql -F owner="${GHREPO%/*}" -F name="${GHREPO#*/}" -F number="$N" -f query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){id number url state baseRefName headRefName headRefOid pullRequestStack{id number entries(first:100){nodes{position pullRequest{number baseRefName headRefName headRefOid state}} pageInfo{hasNextPage endCursor}}} timelineItems(first:100,itemTypes:[BASE_REF_CHANGED_EVENT]){nodes{__typename ... on BaseRefChangedEvent{createdAt previousRefName currentRefName actor{login}}} pageInfo{hasNextPage endCursor}}}}}' >"$OUT/graphql-pr-$N.json" 2>"$OUT/graphql-pr-$N.stderr"; echo $? >"$OUT/graphql-pr-$N.exit"
done
git -C "$REPO" ls-remote --heads origin >"$OUT/remote-refs.txt" 2>"$OUT/remote-refs.stderr"
( cd "$OUT" && find . -type f ! -name MANIFEST.sha256 -print0 | sort -z | xargs -0 shasum -a 256 > MANIFEST.sha256 )
