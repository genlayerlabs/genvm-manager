#!/usr/bin/env python3
"""
Handle a ticked box on the GenVM PR action panel.

Invoked by .github/workflows/branch_pr_actions.yaml on issue_comment:edited
when the edited comment is the panel (it carries the `<!-- genvm-actions -->`
marker). Steps:

1. ignore edits made by a bot (the panel's own reset echo);
2. do nothing unless the PR carries the `ci-safe` label (see "Who may tick a
box" below);
3. parse which boxes are ticked and act:
- "Force run full tests" is a STICKY toggle: on the edit that newly ticks it
we set the `run-full-tests` label (so queue.yaml runs on every future push)
AND dispatch one run now. We do not untick it — its checked state mirrors the
label, and an already-set label means no re-dispatch on unrelated edits.
- "Provision executor PRs" is a momentary button: dispatch
branch_provision_executor_prs.yaml for this PR (force-push each moved executor
line's mirror branch and open its PR), then untick.
4. untick the momentary boxes (not the sticky Force one).

Which action a ticked box maps to is decided by the stable `<!-- action:<id> -->`
marker the panel embeds in each line, NOT by the box's prose. Matching prose
substrings meant rewording a checkbox silently disabled its action, and a new box
whose wording happened to contain another action's keyword would fire that one.
Panels posted before the markers existed fall back to prose matching.

Who may tick a box
------------------
Three independent conditions, all required:

1. the PR carries `ci-safe` — the same label the provision workflow
check before running PR code with credentials in scope, so a target branch
cannot be moved unless the PR is vetted;
2. the edited comment was authored by the bot, i.e. it is the panel we posted;
3. the user who edited it has write access to the repo.

(2) and (3) are both needed, and neither is implied by GitHub's permissions.
It is tempting to reason that "only the comment's author or a write-access user
may edit a comment, so an edit is already authorized" — but the workflow selects
the comment by a MARKER IN ITS BODY, not by who wrote it. Any user may post
their own comment carrying that marker on a public PR and freely edit their own
copy, which without (2) would let a non-collaborator force-push executor mirror
branches. `SENDER` drives (3), and the bot check on it is
only the loop-breaker for the panel's own reset edit.

Runs are started with `gh workflow run queue.yaml --ref <PR head branch>` (a
`workflow_dispatch`), NOT by toggling a label: a label applied by the bot's
GITHUB_TOKEN does not emit a `labeled` event (GitHub blocks recursive runs),
whereas a `workflow_dispatch` from the same token does run. Dispatching on the
PR head branch makes the run's head_sha equal the PR head. Fork PRs have no
head branch in this repo, so the panel can only dispatch for same-repo branches
— the common GenVM flow.

Resetting the panel re-fires issue_comment:edited, but that event is sent by
the bot, so it is ignored — no loop.

Repo and PR number resolve arg > env > default via gh_common; the token is
optional (ambient `gh` auth when unset). Env also: COMMENT_ID, SENDER.
"""

import argparse
import json
import os
import re

import ci_lib
import gh_common
from gh_common import pr_number, repo

CI_SAFE_LABEL = 'ci-safe'
RUN_FULL_TESTS_LABEL = 'run-full-tests'

# Stable ids for the panel's boxes. branch_pr_checklist.yaml embeds one per line
# as `<!-- action:<id> -->`; nothing here depends on the prose beside it, so the
# wording can change freely.
ACTION_FORCE = 'force'
# Compatibility until existing bot panels are refreshed on their next PR event.
ACTION_RERUN = 'rerun'
ACTION_PROVISION = 'provision'

ACTION_MARKER_RE = re.compile(r'<!--\s*action:([a-z-]+)\s*-->')

# Prose fallback for panels posted before the markers existed. Ordered so the
# most specific wins; a line matching none of them maps to no action.
LEGACY_ACTION_KEYWORDS = (
	(ACTION_FORCE, 'force'),
	(ACTION_RERUN, 'rerun'),
	(ACTION_PROVISION, 'provision'),
)

# The one box that is a toggle rather than a button: its ticked state mirrors the
# run-full-tests label, so the reset pass must leave it alone.
STICKY_ACTIONS = frozenset({ACTION_FORCE})


def comment_id():
	return os.environ['COMMENT_ID']


def sender():
	return os.environ['SENDER']


def gh(*args: str, check: bool = True) -> str:
	"""
	`gh` with the manager token, returning stdout.

	`retry=False`: this tool mixes reads with non-idempotent writes (adding a
	label, PATCHing the panel body, dispatching a run) and the retry heuristic
	cannot tell them apart, so replaying one could act twice. Keeps today's
	behaviour; separating the two is GVM-328.
	"""
	return gh_common.gh_manager(*args, check=check, retry=False)


def labels():
	out = gh(
		'api',
		'--paginate',
		f'repos/{repo()}/issues/{pr_number()}/labels?per_page=100',
		'--jq',
		'.[].name',
	)
	return set(out.splitlines())


def add_label(name):
	gh(
		'api',
		'--method',
		'POST',
		f'repos/{repo()}/issues/{pr_number()}/labels',
		'-f',
		f'labels[]={name}',
	)


def head_branch():
	return gh(
		'pr',
		'view',
		pr_number(),
		'--repo',
		repo(),
		'--json',
		'headRefName',
		'--jq',
		'.headRefName',
	).strip()


def dispatch_full_tests(*, release_pipeline_test: bool):
	# Start queue.yaml on the PR head branch so the run's head_sha matches the PR
	# head. Label edits intentionally do not trigger queue.yaml;
	# workflow_dispatch is the only panel-driven entry point.
	branch = head_branch()
	if not branch:
		print('could not resolve PR head branch; cannot dispatch full tests')
		return
	gh(
		'workflow',
		'run',
		'queue.yaml',
		'--repo',
		repo(),
		'--ref',
		branch,
		'-f',
		f'pr={pr_number()}',
		'-f',
		f'release_pipeline_test={str(release_pipeline_test).lower()}',
	)
	print(f'dispatched queue.yaml on `{branch}`')


def dispatch_provision():
	# Momentary: fire the executor-provisioning workflow for this PR. It runs from
	# the default branch (trusted YAML) and checks out the PR head for the scripts
	# behind its own ci-safe guard. A workflow_dispatch from GITHUB_TOKEN runs.
	gh(
		'workflow',
		'run',
		'branch_provision_executor_prs.yaml',
		'--repo',
		repo(),
		'-f',
		f'pr={pr_number()}',
	)
	print(f'dispatched executor provisioning for PR #{pr_number()}')


def action_of(line: str, *, legacy: bool = False) -> str | None:
	"""
	The action a panel line drives, from its `<!-- action:<id> -->` marker.

	`legacy` enables the prose fallback, for panels posted before the markers
	existed — those comments are already on open PRs and cannot be rewritten
	retroactively. Returns None for a line that names no action.
	"""
	m = ACTION_MARKER_RE.search(line)
	if m:
		return m.group(1)
	if not legacy:
		return None
	lowered = line.lower()
	for action, keyword in LEGACY_ACTION_KEYWORDS:
		if keyword in lowered:
			return action
	return None


def is_legacy_panel(body: str) -> bool:
	"""
	Whether this panel predates the `action:` markers.

	Decided per PANEL, not per line. Judging line by line would let the prose
	fallback run on a modern panel — so a checkbox a reviewer adds by hand
	(`- [x] rerun-dependent checks`) could fire `rerun`, which is precisely what
	the markers exist to prevent.
	"""
	return ACTION_MARKER_RE.search(body) is None


TICKED_BOX_RE = re.compile(r'(?m)^\s*-\s*\[[xX]\]\s*(.+?)\s*$')


def ticked_actions(body: str) -> set[str]:
	"""
	The set of action ids whose boxes are ticked.
	"""
	legacy = is_legacy_panel(body)
	found = set()
	for m in TICKED_BOX_RE.finditer(body):
		action = action_of(m.group(1), legacy=legacy)
		if action is not None:
			found.add(action)
	return found


def untick_momentary(body):
	# Untick every ticked box except the sticky ones, whose ticked state mirrors a
	# label rather than requesting a one-off action.
	legacy = is_legacy_panel(body)

	def reset(line):
		if not re.match(r'\s*-\s*\[[xX]\]', line):
			return line
		if action_of(line, legacy=legacy) in STICKY_ACTIONS:
			return line
		return re.sub(r'\[[xX]\]', '[ ]', line, count=1)

	new = '\n'.join(reset(line) for line in body.splitlines())
	if body.endswith('\n'):
		new += '\n'
	if new != body:
		gh(
			'api',
			'--method',
			'PATCH',
			f'repos/{repo()}/issues/comments/{comment_id()}',
			'-f',
			f'body={new}',
		)


class PrActionPanel(ci_lib.Tool):
	"""
	Handle a ticked box on the GenVM PR action panel.
	"""

	def name(self) -> str:
		return 'pr-action-panel'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		gh_common.add_args(parser, executor_repo=False, head_ref=False)

	def handler(self, args: argparse.Namespace) -> int:
		gh_common.set_ctx(gh_common.Ctx.from_args(args))
		run_panel()
		return 0


def run_panel():
	# Ignore the bot's own panel-reset edit (avoids a self-trigger loop).
	if sender().endswith('[bot]'):
		print(f'{sender()} is a bot (panel reset echo); ignoring')
		return

	current = labels()
	if CI_SAFE_LABEL not in current:
		print(f'PR lacks the `{CI_SAFE_LABEL}` label; ignoring panel actions')
		return

	comment = json.loads(
		gh(
			'api',
			f'repos/{repo()}/issues/comments/{comment_id()}',
			'--jq',
			'{body, author: .user.login}',
		)
	)

	# The panel must be the one WE posted. The workflow only matches the marker
	# on the comment body, and anyone may post a comment containing that marker
	# on a public PR and then edit their own copy — which would let a
	# non-collaborator drive these actions. Authorship is what distinguishes the
	# real panel from a forgery.
	author = comment['author']
	if not author.endswith('[bot]'):
		print(f'comment {comment_id()} was authored by `{author}`, not the panel bot;')
		print('ignoring (only the bot-posted action panel drives these actions)')
		return

	# And the editor must be allowed to drive CI. Ticking a box can force-push
	# executor mirror branches, so it is a privileged action.
	if not gh_common.has_write_access(sender()):
		print(f'`{sender()}` has no write access to {repo()}; ignoring panel actions')
		return

	body = comment['body']
	actions = ticked_actions(body)
	if not actions:
		print('no ticked boxes; nothing to do')
		return
	print(f'ticked actions: {", ".join(sorted(actions))}')

	# Force: sticky enable of the run-full-tests marker so every future push
	# runs full tests. Only on the edit that newly sets it do we also dispatch
	# a run now (an already-set label means this is an unrelated edit -> no
	# duplicate run).
	dispatch = False
	if ACTION_FORCE in actions and RUN_FULL_TESTS_LABEL not in current:
		add_label(RUN_FULL_TESTS_LABEL)
		dispatch = True
	if ACTION_RERUN in actions:
		dispatch = True

	if dispatch:
		dispatch_full_tests(
			release_pipeline_test='test-release-pipeline' in current,
		)

	# Provision: momentary -> force-push executor mirror branches and open their PRs.
	if ACTION_PROVISION in actions:
		dispatch_provision()

	untick_momentary(body)


COMMANDS = [PrActionPanel()]
