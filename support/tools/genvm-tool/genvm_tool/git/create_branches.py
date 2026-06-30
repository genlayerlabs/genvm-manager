"""`genvm-tool git create-branches` — branch the sub-repos that have new content.

Surveys the manager and every checked-out executor submodule, keeps the ones
that carry uncommitted or unpushed work, lets you tick which of those to branch,
and creates one identically-named branch (`git checkout -b`) across all picked
repos. The shared branch name keeps the manager and its executor submodule in
lockstep, matching the `v<X>-dev` / same-named-mirror convention CI relies on.
"""

import re
import subprocess

from .. import common

NAME = 'create-branches'
HELP = 'create a same-named branch across selected sub-repos that have new content'

# Branches that are part of the release/branch model rather than a feature branch
# worth propagating: `main`, a version branch (`v0.3.x`), or a dev branch
# (`v0.3-dev`). Used only to decide whether to offer the current branch as the
# prompt default — never to filter what the user may type.
_MODEL_BRANCH = re.compile(r'main|v[\d.]+\.x|v[\d.]+-dev')


def configure(parser):
	parser.add_argument(
		'name',
		nargs='?',
		help='branch name to create across the selected repos (prompted if omitted); '
		'must not contain spaces',
	)
	parser.add_argument(
		'--repo',
		default='all',
		help="limit candidates: 'all' (default), 'manager', or an executor name "
		'like executors/v0.3.x',
	)


def _git(repo: common.Repo, *args: str) -> subprocess.CompletedProcess:
	return subprocess.run(
		['git', '-C', str(repo.path), *args],
		capture_output=True,
		text=True,
	)


def _new_content(repo: common.Repo) -> str | None:
	"""Describe a repo's pending work, or None if it has nothing to branch.

	"New content" is a dirty working tree (staged / unstaged / untracked) or
	commits ahead of the upstream branch — anything a fresh branch would capture.
	"""
	changed = [
		ln for ln in _git(repo, 'status', '--porcelain').stdout.splitlines() if ln.strip()
	]
	# `@{upstream}` errors out when the branch has no upstream; treat that as 0.
	ahead = 0
	r = _git(repo, 'rev-list', '--count', '@{upstream}..HEAD')
	if r.returncode == 0 and r.stdout.strip():
		ahead = int(r.stdout.strip())

	if not changed and ahead == 0:
		return None
	parts = []
	if changed:
		parts.append(f'{len(changed)} change(s)')
	if ahead:
		parts.append(f'{ahead} commit(s) ahead')
	return ', '.join(parts)


def _validate_name(name: str) -> bool | str:
	"""questionary-style validator: True when ok, else the error message."""
	if not name:
		return 'branch name must not be empty'
	if ' ' in name:
		return 'branch name must not contain spaces'
	return True


def _branch_exists(repo: common.Repo, name: str) -> bool:
	return (
		_git(repo, 'show-ref', '--verify', '--quiet', f'refs/heads/{name}').returncode == 0
	)


def _current_branch(repo: common.Repo) -> str:
	"""The repo's checked-out branch, or '' when detached."""
	return _git(repo, 'branch', '--show-current').stdout.strip()


def _default_name(repo: common.Repo) -> str:
	"""The manager's current branch as a prompt default, unless it is a release/
	branch-model branch (main / version / dev) or detached — then no default.
	"""
	cur = _current_branch(repo)
	if not cur or _MODEL_BRANCH.fullmatch(cur):
		return ''
	return cur


def main(ctx: common.Context, args) -> int:
	import questionary

	# Fail fast on a bad CLI name before bothering with the survey/prompt.
	if args.name is not None:
		verdict = _validate_name(args.name)
		if verdict is not True:
			raise common.ToolError(verdict)

	# Survey candidates: checked-out repos that carry something worth branching.
	candidates = []
	for repo in common.discover_repos(ctx.root, args.repo):
		if not repo.is_checked_out():
			ctx.logger.debug('skipping repo (not checked out)', repo=repo.name)
			continue
		summary = _new_content(repo)
		if summary is None:
			ctx.logger.debug('skipping repo (no new content)', repo=repo.name)
			continue
		candidates.append((repo, summary))

	if not candidates:
		ctx.printer.put('no sub-repos have new content to branch')
		return 0

	# Tick which candidates to branch (all pre-checked).
	selected = questionary.checkbox(
		'Select sub-repos to branch',
		choices=[
			questionary.Choice(f'{repo.name} — {summary}', value=repo, checked=True)
			for repo, summary in candidates
		],
	).ask()
	# `ask()` returns None on Ctrl-C/Esc; an empty list means everything unticked.
	if not selected:
		ctx.printer.put('nothing selected; no branches created')
		return 0

	# Branch name: the CLI value (already validated above) or an interactive prompt
	# defaulted to the manager's current feature branch (e.g. while mid-work on it).
	name = args.name
	if name is None:
		default = _default_name(common.discover_repos(ctx.root, 'manager')[0])
		name = questionary.text(
			'Branch name', default=default, validate=_validate_name
		).ask()
		if not name:
			ctx.printer.put('no branch name given; no branches created')
			return 0

	# A repo already sitting on `name` needs nothing done — that is not a clash
	# (e.g. the manager you started this branch on). Leave it exactly as it is.
	on_branch = [repo for repo in selected if _current_branch(repo) == name]
	todo = [repo for repo in selected if repo not in on_branch]

	# Pre-flight: refuse if `name` already exists (as a ref) in any repo we would
	# create it in, so we never half-create the set — and never reset/move an
	# existing branch. Repos already on `name` are exempt (handled above).
	clash = [repo.name for repo in todo if _branch_exists(repo, name)]
	if clash:
		raise common.ToolError(f'branch {name!r} already exists in: {", ".join(clash)}')

	for repo in on_branch:
		ctx.printer.put('already on branch', repo=repo.name, branch=name)

	for repo in todo:
		# `checkout -b` only creates a new ref and moves HEAD onto the same commit;
		# it never touches the working tree, so pending work is carried, not lost.
		r = _git(repo, 'checkout', '-b', name)
		if r.returncode != 0:
			raise common.ToolError(
				f'failed to create branch {name!r} in {repo.name}: {r.stderr.strip()}'
			)
		ctx.printer.put('created branch', repo=repo.name, branch=name)
	return 0
