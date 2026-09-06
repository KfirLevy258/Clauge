#!/bin/sh
# Remove agent trailers from commits that already exist.
#
#   tools/strip_agent_trailers.sh <upstream>      # e.g. origin/main
#
# .githooks/commit-msg strips `Co-Authored-By: Claude` and `Claude-Session:`
# from every NEW commit. It cannot touch commits made before it was installed,
# or made in a clone that never ran `git config core.hooksPath .githooks`, and
# this repository is published at github.com/KfirLevy258/Blink. The last time
# such commits reached a release it took a history rewrite to undo, in a hurry.
#
# This does that rewrite deliberately, before the push rather than after:
# rewrites every commit in <upstream>..HEAD, keeping trees, authors, dates,
# parents and merges exactly as they are, and changing nothing but the message.
#
# It REWRITES HISTORY. Only ever on commits that have not been pushed --
# rewriting published commits changes shas other people already have. It
# refuses if any commit in the range is already on the upstream branch.
#
# A backup ref is left behind, so the old history is one command away:
#   git reset --hard <the refs/backup/... ref this prints>
set -eu

UPSTREAM="${1:-}"
[ -n "$UPSTREAM" ] || { echo "usage: $0 <upstream>   (e.g. origin/main)" >&2; exit 2; }

ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"

git rev-parse --verify --quiet "$UPSTREAM" >/dev/null || {
	echo "FATAL: $UPSTREAM is not a ref in this repository." >&2; exit 1; }

[ -z "$(git status --porcelain)" ] || {
	echo "FATAL: working tree is dirty. A rewrite must start from a clean" >&2
	echo "       tree, or uncommitted work is what gets confusing." >&2
	exit 1; }

RANGE="$UPSTREAM..HEAD"
COUNT=$(git rev-list --count "$RANGE")
[ "$COUNT" -gt 0 ] || { echo "Nothing in $RANGE. Nothing to do."; exit 0; }

# Which of them actually carry a trailer. Reported before anything is touched:
# a rewrite of forty commits to fix three should say so out loud.
PATTERN='^[[:space:]]*(Co-Authored-By:[[:space:]]*Claude|Claude-Session:)'
DIRTY=$(git log --format='%H' "$RANGE" | while read -r sha; do
	if git log -1 --format='%B' "$sha" | grep -qiE "$PATTERN"; then
		echo "$sha"
	fi
done)

if [ -z "$DIRTY" ]; then
	echo "No agent trailers in $COUNT commit(s) across $RANGE. Nothing to do."
	exit 0
fi

echo "Agent trailers in $(printf '%s\n' "$DIRTY" | wc -l | tr -d ' ') of $COUNT commit(s):"
printf '%s\n' "$DIRTY" | while read -r sha; do
	git log -1 --format='  %h %s' "$sha"
done
echo
echo "Rewriting $COUNT commit(s) in $RANGE. Trees, authors, dates and merges"
echo "are preserved; only messages change."
echo

# Not pushed anywhere: rewriting a published commit changes shas that other
# clones already have, and the fix for that is worse than the problem.
PUSHED=$(git branch -r --contains HEAD 2>/dev/null | head -1 || true)
[ -z "$PUSHED" ] || {
	echo "FATAL: HEAD is already on a remote branch ($PUSHED)." >&2
	echo "       Rewriting published history changes shas other clones have." >&2
	exit 1; }

BACKUP="refs/backup/pre-strip-$(date +%Y%m%d-%H%M%S)"
git update-ref "$BACKUP" HEAD
echo "Backup ref: $BACKUP"
echo

# filter-branch prints a warning steering people to filter-repo. It is right in
# general and wrong here: this needs no new tool on the machine, touches only
# messages, and runs once.
FILTER_BRANCH_SQUELCH_WARNING=1 \
git filter-branch -f --msg-filter "grep -viE '$PATTERN' || true" -- "$RANGE"

echo
LEFT=$(git log --format='%B' "$RANGE" | grep -icE "$PATTERN" || true)
if [ "$LEFT" = "0" ]; then
	echo "Done. No agent trailers remain in $RANGE."
	echo "Undo with: git reset --hard $BACKUP"
else
	echo "FAILED: $LEFT trailer line(s) still present. History unchanged is at"
	echo "        $BACKUP -- reset to it and investigate before pushing." >&2
	exit 1
fi
