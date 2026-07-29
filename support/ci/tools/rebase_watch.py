#!/usr/bin/env python3
"""
Flag open PRs whose branch has fallen behind its `v<X>-dev` base.

`initial / behind-check` (incl_initial.yaml) already asserts 0-behind, but it only
runs when the PR is pushed — a PR goes stale when the BASE moves, and nothing
re-evaluates it then. So a PR can sit green for days and only discover at Merge
time that it is unmergeable (the Merge action fast-forwards, so behind > 0 is
fatal there).

This tool re-evaluates on the other edge: every push to a dev branch sweeps the
PRs targeting it. It is deliberately advisory — it posts a check-run and a label,
and both are cosmetic. Nothing here gates a merge: the authoritative 0-behind
check stays in `initial` and in genvm_merge_into_dev. The point is that the PR
page turns red the moment the branch goes stale rather than at merge time.

Two surfaces, because they are visible in different places:

- a `not rebased` check-run on the PR head — red on the PR page, with the exact
	`git` command to fix it;
- a red `not rebased` label — the only one of the two visible in the PR *list*.

Both are cleared as soon as the PR is 0 behind again. "Behind" comes from
`behind.behind_by_api` — the same measurement the enforcing checks use, via the
compare API, so nothing is checked out and fork PRs work the same as branch PRs.

Repo and PR number resolve arg > env > default via gh_common; the token is
optional (ambient `gh` auth when unset).
"""

import argparse
import json
import re
import tempfile

import ci_lib
import gh_common

# The function, not the module: `sweep` has a local named `behind`, and a module
# of that name would be shadowed by it.
from behind import behind_by_api
from gh_common import gh_manager as gh
from gh_common import repo

LABEL = 'not rebased'
LABEL_COLOR = 'b60205'
LABEL_DESCRIPTION = 'branch is behind its base; rebase before it can be merged'

CHECK_NAME = 'not rebased'
# Stamped on every check-run this tool creates, so it can find its own again
# without trusting the name (which any app may also use).
CHECK_EXTERNAL_ID = 'genvm-ci/rebase-watch'

# Bases this sweeps. The Merge action only ever fast-forwards onto a dev branch,
# so a PR targeting anything else has no 0-behind requirement to report on.
DEV_BASE_RE = re.compile(r'v.*-dev')


def pr_state(number: int | str) -> dict:
	"""
	Current number/base/head/state of one PR.
	"""
	return json.loads(
		gh(
			'api',
			f'repos/{repo()}/pulls/{number}',
			'--jq',
			'{number, base: .base.ref, head: .head.sha, state}',
		)
	)


def open_prs(base: str | None, pr_number: str | None) -> list[dict]:
	"""
	The PRs to evaluate: one explicit PR, or every open PR on a dev base.

	`--pr` wins so the PR-side triggers (a push to the PR, a retarget) can refresh
	just that PR instead of re-sweeping the repo. An explicit PR is returned even
	when its base is not a dev branch: `sweep` still has to CLEAR a flag it set
	before the PR was retargeted off one.
	"""
	if pr_number is not None:
		pr = pr_state(pr_number)
		return [pr] if pr['state'] == 'open' else []

	prs = [
		json.loads(line)
		for line in gh(
			'api',
			'--paginate',
			f'repos/{repo()}/pulls?state=open&per_page=100',
			'--jq',
			'.[] | {number, base: .base.ref, head: .head.sha}',
		).splitlines()
		if line.strip()
	]
	if base is not None:
		return [pr for pr in prs if pr['base'] == base]
	return [pr for pr in prs if DEV_BASE_RE.fullmatch(pr['base'])]


def behind_by(base: str, head_sha: str) -> int:
	"""
	Commits in `base` that `head_sha` does not contain.
	"""
	return behind_by_api(repo(), base, head_sha, token=gh_common.manager_token())


def ensure_label() -> None:
	"""
	Create the label if it is missing. Already-exists (422) is the normal case.
	"""
	gh_common.gh(
		'api',
		'--method',
		'POST',
		f'repos/{repo()}/labels',
		'-f',
		f'name={LABEL}',
		'-f',
		f'color={LABEL_COLOR}',
		'-f',
		f'description={LABEL_DESCRIPTION}',
		token=gh_common.manager_token(),
		check=False,
	)


def set_label(
	pr: int, present: bool, had: bool, expect_head: str, expect_base: str
) -> None:
	"""
	Add or remove the label, but only when it actually changes.

	A no-op add still writes a timeline event and notifies subscribers, and this
	tool runs on every push to a dev branch — so re-asserting the current state on
	every sweep would spam every open PR.

	The label is the one surface NOT keyed by commit (a check-run lives on its own
	sha, so a stale one is invisible), which makes it the only thing a base-push
	sweep and a PR-push sweep can fight over. Both coordinates are re-read before
	writing, because either can move underneath a sweep:

	- the head: a base sweep reads head H0, the author rebases to H1, the PR
	sweep clears the label, and the base sweep would re-add it for a commit that
	no longer exists;
	- the base: a base sweep computes "behind", the PR is retargeted off that dev
	branch (its head unchanged, so a head-only guard passes), the retarget run
	clears the flag, and the base sweep would re-add it — permanently, since no
	later sweep of that base will ever see this PR again.

	In both cases the run for the NEW state has already flagged it correctly.
	"""
	if present == had:
		return
	current = pr_state(pr)
	if current['head'] != expect_head:
		print(f'#{pr}: head moved while evaluating; leaving the label to that run')
		return
	if current['base'] != expect_base:
		print(
			f'#{pr}: base changed to `{current["base"]}` while evaluating; '
			f'leaving the label to that run'
		)
		return
	if present:
		gh_common.gh(
			'api',
			'--method',
			'POST',
			f'repos/{repo()}/issues/{pr}/labels',
			'-f',
			f'labels[]={LABEL}',
			token=gh_common.manager_token(),
			check=False,
		)
	else:
		# 404 when the label is already gone (a concurrent sweep, a manual removal).
		gh_common.gh(
			'api',
			'--method',
			'DELETE',
			f'repos/{repo()}/issues/{pr}/labels/{LABEL}',
			token=gh_common.manager_token(),
			check=False,
		)


def has_label(pr: int) -> bool:
	names = gh(
		'api', '--paginate', f'repos/{repo()}/issues/{pr}/labels', '--jq', '.[].name'
	).splitlines()
	return LABEL in [n.strip() for n in names]


def existing_check_id(head_sha: str) -> int | None:
	"""
	Our own check-run on `head_sha`, if we already posted one.

	Matched on `external_id`, not on the name: a name is not a namespace, so
	another app could hold a check-run called `not rebased` and we would PATCH
	(or, lacking its app's token, fail to PATCH) a check that is not ours. The
	API defaults to `filter=latest`, so this returns the one we would replace —
	updating in place keeps the PR's checks list to a single entry rather than one
	per dev-branch push.

	`api_path`, not an f-string: `CHECK_NAME` contains a space, and `gh api`
	forwards a query string verbatim — an unencoded space makes the request
	**hang** rather than fail, which would wedge this job until its timeout.
	"""
	ids = gh(
		'api',
		gh_common.api_path(
			f'repos/{repo()}/commits/{head_sha}/check-runs',
			check_name=CHECK_NAME,
			per_page=100,
		),
		'--jq',
		f'.check_runs[] | select(.external_id == "{CHECK_EXTERNAL_ID}") | .id',
	).splitlines()
	return int(ids[0]) if ids else None


def post_check(head_sha: str, base: str, behind: int, *, stale: bool = False) -> None:
	"""
	Create or update the advisory check-run on the PR head.

	`stale` means the PR no longer targets a dev branch, so the check has nothing
	to say — but it is only cleared, never freshly posted, so PRs that never had
	one stay untouched.
	"""
	check_id = existing_check_id(head_sha)
	if stale:
		if check_id is None:
			return
		conclusion = 'neutral'
		title = f'not applicable — base `{base}` is not a dev branch'
		summary = f'`{base}` is not merged by fast-forward, so being behind it is fine.'
	elif behind:
		conclusion = 'failure'
		title = f'⛔ {behind} commit(s) behind {base} — rebase'
		summary = (
			f'This branch is **{behind} commit(s) behind `{base}`**.\n\n'
			f'The Merge action only fast-forwards, so it will refuse this branch until '
			f'it contains the base tip:\n\n'
			f'```sh\ngit fetch origin\ngit rebase origin/{base}\ngit push --force-with-lease\n```\n\n'
			f'(Advisory: this check does not block anything. `initial / behind-check` '
			f'and the Merge action are what actually enforce it.)'
		)
	else:
		conclusion = 'success'
		title = f'up to date with {base}'
		summary = f'This branch contains the tip of `{base}`.'

	payload = {
		'name': CHECK_NAME,
		'head_sha': head_sha,
		'external_id': CHECK_EXTERNAL_ID,
		'status': 'completed',
		'conclusion': conclusion,
		'output': {'title': title, 'summary': summary},
	}
	if check_id is None:
		method, path = 'POST', f'repos/{repo()}/check-runs'
	else:
		method, path = 'PATCH', f'repos/{repo()}/check-runs/{check_id}'
		payload.pop('head_sha')

	# Via a file rather than `-f` fields: the payload nests (`output.title`), which
	# `gh api -f` cannot express, and `--input -` would need stdin that gh_common.gh
	# does not plumb.
	with tempfile.NamedTemporaryFile('w', suffix='.json') as body:
		json.dump(payload, body)
		body.flush()
		gh_common.gh(
			'api',
			'--method',
			method,
			path,
			'--input',
			body.name,
			token=gh_common.manager_token(),
		)


def sweep(base: str | None, pr_number: str | None, dry_run: bool) -> None:
	prs = open_prs(base, pr_number)
	if not prs:
		print('no open PRs to evaluate')
		return
	if not dry_run:
		ensure_label()
	for pr in prs:
		# A PR retargeted off a dev branch keeps whatever flag it was last given,
		# and no later push sweep will ever see it again — so clear it here. Only
		# clearing, never setting: a PR that never carried the flag stays untouched.
		stale = not DEV_BASE_RE.fullmatch(pr['base'])
		behind = 0 if stale else behind_by(pr['base'], pr['head'])
		if stale:
			state = f'base `{pr["base"]}` is not a dev branch; clearing'
		else:
			state = f'{behind} behind' if behind else 'up to date'
		print(f'#{pr["number"]} ({pr["base"]}): {state}')
		if dry_run:
			continue
		had = has_label(pr['number'])
		post_check(pr['head'], pr['base'], behind, stale=stale)
		set_label(pr['number'], behind > 0, had, pr['head'], pr['base'])


class RebaseWatch(ci_lib.Tool):
	"""
	Flag open PRs that have fallen behind their dev base (advisory only).
	"""

	def name(self) -> str:
		return 'rebase-watch'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		gh_common.add_args(parser, executor_repo=False, head_ref=False)
		parser.add_argument(
			'--base',
			default=None,
			help='only sweep PRs targeting this base branch (default: every v<X>-dev base)',
		)
		parser.add_argument(
			'--dry-run',
			action='store_true',
			help='report how far behind each PR is without posting a check or label',
		)

	def handler(self, args: argparse.Namespace) -> int:
		ctx = gh_common.set_ctx(gh_common.Ctx.from_args(args))
		sweep(args.base, ctx.pr_number_opt, args.dry_run)
		return 0


COMMANDS = [RebaseWatch()]
