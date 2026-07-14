#!/usr/bin/env bash
# Check the commit message of every commit a PR adds, in the manager and in each
# executor submodule.
#
# The `check-commit-message` hook is a commit-msg-stage hook, so the CI hook run
# (`pre-commit run --all-files --hook-stage pre-commit`) never fires it: it would
# have nothing to check anyway, since only the tree is checked out, not the
# messages. Each repo is checked with its OWN copy of the script, so a repo that
# tightens its rules is not linted against the manager's copy.
#
# $CHANGES is the JSON emitted by support/scripts/ci-changes.py (the `changes`
# output of the get-src action).
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)
cd "$SCRIPT_DIR/../../.."

if [ -z "${CHANGES:-}" ]; then
	echo "::error::CHANGES is empty; it must hold the get-src action's \`changes\` output"
	exit 1
fi

rc=0

while IFS=$'\t' read -r dir base head; do
	[ -n "$dir" ] || continue

	label="$dir"
	[ "$dir" = "." ] && label="manager"

	checker="$dir/support/scripts/check-commit-message.py"
	if [ ! -f "$checker" ]; then
		echo "::error::$label has no support/scripts/check-commit-message.py"
		rc=1
		continue
	fi

	# A submodule is cloned at its gitlink, so the base commit is not guaranteed
	# to be present; fetch the branches so `rev-list` can walk the range.
	if ! git -C "$dir" cat-file -e "$base^{commit}" 2>/dev/null; then
		git -C "$dir" fetch --no-tags --force origin '+refs/heads/*:refs/remotes/origin/*'
	fi

	echo "::group::commit messages: $label ($base..$head)"
	commits=$(git -C "$dir" rev-list "$base..$head")
	if [ -z "$commits" ]; then
		echo "no new commits"
	fi
	for sha in $commits; do
		subject=$(git -C "$dir" log -1 --format=%s "$sha")
		message=$(git -C "$dir" log -1 --format=%B "$sha")
		if python3 "$checker" --message-text "$message"; then
			echo "ok   $sha $subject"
		else
			echo "::error::$label $sha: bad commit message: $subject"
			rc=1
		fi
	done
	echo "::endgroup::"
done < <(
	python3 - <<'PY'
import json, os

for path, info in json.loads(os.environ['CHANGES']).items():
    if not info['has_changes']:
        continue
    # An added/removed submodule has no range to walk in this repo.
    if not info['base_commit'] or not info['branch_commit']:
        continue
    print('\t'.join((path, info['base_commit'], info['branch_commit'])))
PY
)

exit $rc
