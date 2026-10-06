#!/usr/bin/env python3
"""
One definition of "how far behind its base is this branch".

Repository policy requires a PR head to contain its base tip before it enters
the App-owned merge train, so "0 commits behind base" appears in 2 places:

- `incl_initial.yaml`'s always-on gate, backed by `pipelines/checks.py`, which
	fails a PR the moment it falls behind;
- `tools/rebase_watch.py`, the advisory check-run/label sweep that re-evaluates
	when the BASE moves;

The callers used to compute it separately, so they could disagree about the
same PR. The measurement lives here
once, in the two flavours the callers genuinely need:

- `behind_by_api` for callers with no checkout (a `pull_request_target` sweep);
- `behind_by_git` for callers that already have the objects local.

Both answer the same question with the same meaning: the number of commits
reachable from `base` that `head` does not contain. Zero means the head
contains the base tip.
"""

import json
import subprocess
from pathlib import Path

import ci_lib
import gh_common


def behind_by_api(repo: str, base: str, head_sha: str, token: str | None = None) -> int:
	"""
	Commits in `base` that `head_sha` does not contain, via the compare API.

	Compares by sha rather than by `owner:branch`, so a fork PR resolves exactly
	as a branch PR does — GitHub keeps every PR head in the base repo. Needs no
	checkout, which is what lets a `pull_request_target` job call it.
	"""
	return json.loads(
		gh_common.gh(
			'api',
			f'repos/{repo}/compare/{base}...{head_sha}',
			'--jq',
			'.behind_by',
			token=token,
		).stdout
	)


def behind_by_git(base_ref: str, head_ref: str, *, cwd: Path | None = None) -> int:
	"""
	Commits in `base_ref` that `head_ref` does not contain, via local objects.

	Both refs must already be present locally; the caller is responsible for
	fetching them (see the `behind-check` pipeline for the refspecs that work
	for fork PRs).

	Defaults to the repo root rather than the process CWD, matching `ci_lib.run`
	— a caller that fetches through `ci_lib.run` (into ROOT_DIR) and then counts
	here must be looking at the same repository.
	"""
	out = subprocess.run(
		['git', 'rev-list', '--count', f'{head_ref}..{base_ref}'],
		check=True,
		text=True,
		capture_output=True,
		cwd=cwd or ci_lib.ROOT_DIR,
	).stdout.strip()
	return int(out)
