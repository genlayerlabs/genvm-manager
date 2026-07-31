"""`genvm-tool git check-for-push` — pre-push status across the sub-repos.

Read-only survey of the manager and every executor submodule. For each repo it
reports whether it is dirty, on a branch, committed into the manager, and in sync
with `origin`, folds that into a `ready` verdict, and prints the next action in
the add → commit → push pipeline. After the per-repo rows it emits one
`suggested_push_command` that pushes the ready repos in order (or `none` if any
repo is not ready). Exits non-zero when any repo is not ready.

By default it queries `origin` live (`git ls-remote`); pass `--offline` to compare
against your last-fetched `origin/<branch>` instead, with no network. This never
fetches, resets, switches, or writes anything.
"""

import shlex
import subprocess

from .. import common

NAME = 'check-for-push'
HELP = 'read-only pre-push status + suggested action across manager and submodules'

REMOTE = 'origin'


def configure(parser):
	parser.add_argument(
		'--repo',
		default='all',
		help="'all' (default), 'manager', or an executor name like executors/v0.3.x",
	)
	parser.add_argument(
		'--offline',
		action='store_true',
		help='compare against the local origin/<branch> tracking ref (last fetch) '
		'instead of querying the remote live',
	)


def _git(repo: common.Repo, *args: str) -> subprocess.CompletedProcess:
	return common.git(repo, *args)


def _current_branch(repo: common.Repo) -> str:
	"""The repo's checked-out branch, or '' when detached."""
	return _git(repo, 'branch', '--show-current').stdout.strip()


def _origin_url(repo: common.Repo) -> str:
	return common.remote_url(repo, REMOTE)


def _worktree_state(
	repo: common.Repo, ignore_submodules: bool
) -> tuple[int, bool, bool]:
	"""Porcelain summary: (changed-line count, has-staged, has-unstaged/untracked).

	The manager passes ``--ignore-submodules=dirty`` so a submodule's own dirty
	tree is left to that submodule's row, while a *moved gitlink* (new commits)
	still counts — that genuinely needs an add + commit in the manager.
	"""
	args = ['status', '--porcelain']
	if ignore_submodules:
		args.append('--ignore-submodules=dirty')
	lines = [ln for ln in _git(repo, *args).stdout.splitlines() if len(ln) >= 2]
	# Porcelain columns: X = index (staged), Y = worktree. '??' is untracked.
	staged = any(ln[0] not in ' ?' for ln in lines)
	unstaged = any(ln[1] != ' ' for ln in lines)
	return len(lines), staged, unstaged


def _committed_in_manager(
	manager: common.Repo, rel: str, sub: common.Repo
) -> tuple[str, bool]:
	"""Whether the submodule's HEAD is the gitlink committed in the manager."""
	recorded = _git(manager, 'rev-parse', f'HEAD:{rel}')
	if recorded.returncode != 0:
		return 'no (not committed in manager HEAD)', False
	rec = recorded.stdout.strip()
	head = _git(sub, 'rev-parse', 'HEAD').stdout.strip()
	if rec == head:
		return 'yes', True
	return f'no (manager has {rec[:10]}, submodule at {head[:10]})', False


def _launch_origin_queries(targets: list[tuple[common.Repo, str]]):
	"""Start one parallel `ls-remote` per distinct origin URL.

	`targets` is (repo, branch) for the repos we want a live remote answer for.
	Repos that share an origin URL (the executor submodules) are batched into a
	single multi-ref `ls-remote`. Returns a handle for :func:`_collect_origin_queries`;
	this only spawns the processes — it does not wait — so the network overlaps
	whatever the caller does next.
	"""
	groups: dict[str, dict] = {}
	# repo.name -> (hash, reachable); pre-seed repos that have no origin remote.
	result: dict[str, tuple[str | None, bool]] = {}
	for repo, branch in targets:
		url = _origin_url(repo)
		if not url:
			result[repo.name] = (None, False)
			continue
		g = groups.setdefault(url, {'repo': repo, 'branches': set(), 'members': []})
		g['branches'].add(branch)
		g['members'].append((repo, branch))

	env = common.git_env(**common.GIT_NETWORK_ENV)
	procs = []
	for g in groups.values():
		refs = [f'refs/heads/{b}' for b in sorted(g['branches'])]
		proc = subprocess.Popen(
			['git', '-C', str(g['repo'].path), 'ls-remote', REMOTE, *refs],
			stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL,
			text=True,
			env=env,
		)
		procs.append((g, proc))
	return procs, result


def _collect_origin_queries(launched) -> dict[str, tuple[str | None, bool]]:
	"""Wait for the launched `ls-remote` processes and map each repo to its remote
	branch hash: `{repo.name: (hash | None, reachable)}` (None hash = branch absent).
	"""
	procs, result = launched
	for g, proc in procs:
		try:
			out, _ = proc.communicate(timeout=common.GIT_REMOTE_TIMEOUT)
			ok = proc.returncode == 0
		except subprocess.TimeoutExpired:
			proc.kill()
			proc.communicate()
			out, ok = '', False
		hashes = {}
		if ok:
			for line in out.splitlines():
				sha, _, ref = line.partition('\t')
				if ref.startswith('refs/heads/'):
					hashes[ref[len('refs/heads/') :]] = sha
		for repo, branch in g['members']:
			result[repo.name] = (hashes.get(branch), True) if ok else (None, False)
	return result


def _kind_from_counts(ahead: int, behind: int) -> str:
	if ahead and behind:
		return 'diverged'
	if ahead:
		return 'ahead'
	if behind:
		return 'behind'
	return 'synced'


def _origin_state_remote(
	repo: common.Repo, branch: str, lookup: dict[str, tuple[str | None, bool]]
) -> tuple[str, str]:
	"""Resolve origin status from the pre-fetched live `ls-remote` answer."""
	if not branch:
		return 'no (detached)', 'detached'
	rhash, reachable = lookup.get(repo.name, (None, False))
	if not reachable:
		return 'unknown (origin unreachable)', 'unreachable'
	if rhash is None:
		return f'no (no origin/{branch})', 'absent'
	head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
	if rhash == head:
		return 'yes', 'synced'
	# Differ: count precisely only if the remote commit is already in our object
	# store (e.g. the remote is behind us, so its tip is an ancestor of HEAD).
	if _git(repo, 'cat-file', '-e', f'{rhash}^{{commit}}').returncode == 0:
		ahead = int(_git(repo, 'rev-list', '--count', f'{rhash}..HEAD').stdout or 0)
		behind = int(_git(repo, 'rev-list', '--count', f'HEAD..{rhash}').stdout or 0)
		return f'no (ahead {ahead}, behind {behind})', _kind_from_counts(ahead, behind)
	# Remote holds commits we lack — counts need a fetch first.
	return f'no (differs; origin at {rhash[:10]}, fetch to compare)', 'differs'


def _origin_state_local(repo: common.Repo, branch: str) -> tuple[str, str]:
	"""Compare HEAD to the local `origin/<branch>` tracking ref (no network)."""
	if not branch:
		return 'no (detached)', 'detached'
	ref = f'refs/remotes/origin/{branch}'
	if _git(repo, 'rev-parse', '--verify', '--quiet', ref).returncode != 0:
		return f'no (no local origin/{branch})', 'absent'
	counts = _git(
		repo, 'rev-list', '--left-right', '--count', f'{ref}...HEAD'
	).stdout.split()
	if len(counts) != 2:
		return 'no (cannot compare)', 'differs'
	behind, ahead = int(counts[0]), int(counts[1])
	if ahead == 0 and behind == 0:
		return 'yes', 'synced'
	return f'no (ahead {ahead}, behind {behind})', _kind_from_counts(ahead, behind)


def _suggest(detached: bool, staged: bool, unstaged: bool, origin_kind: str) -> str:
	"""The next step in the add → commit → push pipeline for this repo."""
	if detached:
		return 'switch to a branch (detached HEAD)'
	if unstaged:
		return 'add files'
	if staged:
		return 'commit files'
	return {
		'absent': 'push (no origin branch yet)',
		'ahead': 'push',
		'behind': 'pull (behind origin)',
		'diverged': 'reconcile (diverged from origin)',
		'differs': 'fetch (cannot compare with origin)',
		'unreachable': 'fetch (origin unreachable)',
		'synced': 'none (ready to push)',
	}.get(origin_kind, 'fetch (cannot compare with origin)')


def _suggested_push_command(repo: common.Repo, branch: str, ready: bool) -> str | None:
	"""A copy-pasteable shell command for pushing the current branch."""
	if not ready or not branch:
		return None
	return f'cd {shlex.quote(str(repo.path))} && git push origin {shlex.quote(branch)}'


def main(ctx: common.Context, args) -> int:
	manager = common.discover_repos(ctx.root, 'manager')[0]
	manager_branch = _current_branch(manager)

	# Cheap local pass: checked-out state + current branch (needed to know which
	# refs to query on the remote).
	entries = []
	for repo in common.discover_repos(ctx.root, args.repo):
		checked_out = repo.is_checked_out()
		branch = _current_branch(repo) if checked_out else ''
		entries.append((repo, checked_out, branch))

	# Fire the remote queries first (parallel, one per origin URL) so they run
	# while we gather local status below and before anything is printed.
	launched = None
	if not args.offline:
		launched = _launch_origin_queries(
			[
				(repo, branch)
				for repo, checked_out, branch in entries
				if checked_out and branch
			]
		)

	# Local status, overlapping the in-flight network calls.
	local: dict[str, dict] = {}
	for repo, checked_out, branch in entries:
		if not checked_out:
			continue
		is_sub = repo.rel != ''
		changed, staged, unstaged = _worktree_state(repo, ignore_submodules=not is_sub)
		row = {'changed': changed, 'staged': staged, 'unstaged': unstaged}
		if is_sub:
			row['committed_msg'], row['committed_ok'] = _committed_in_manager(
				manager, repo.rel, repo
			)
		local[repo.name] = row

	# Present executor repos first so the manager row naturally comes last, matching
	# the push order in the submodules guide.
	entries.sort(key=lambda item: item[0].rel == '')

	lookup = _collect_origin_queries(launched) if launched is not None else {}

	all_ready = True
	push_commands: list[str] = []
	for repo, checked_out, branch in entries:
		if not checked_out:
			# Not checked out: nothing local to push, and nothing we can verify.
			ctx.printer.put(
				repo.name,
				checked_out='no',
				action='checkout submodule',
			)
			continue

		is_sub = repo.rel != ''
		detached = not branch
		row = local[repo.name]
		if args.offline:
			sync_msg, kind = _origin_state_local(repo, branch)
		else:
			sync_msg, kind = _origin_state_remote(repo, branch, lookup)

		ready = row['changed'] == 0 and not detached and kind in {'synced', 'absent'}
		fields = {
			'branch': branch or 'DETACHED',
			'dirty': f'yes ({row["changed"]})' if row['changed'] else 'no',
			'on_branch': 'yes' if branch else 'no (detached)',
		}
		if is_sub:
			# A submodule mirrors the manager branch under the line-namespaced
			# convention (`feat/x` on the manager -> `pr/v0.3/feat/x` here), so
			# compare against the manager branch mapped through feature_branch,
			# not the literal string.
			mirror = repo.feature_branch(manager_branch) if manager_branch else ''
			same = bool(branch) and branch == mirror
			fields['same_branch_as_manager'] = (
				'yes'
				if same
				else f'no (manager on {manager_branch or "DETACHED"}, expected {mirror or "—"})'
			)
			fields['committed_in_manager'] = row['committed_msg']
			ready = ready and row['committed_ok']

		fields['in_sync_with_origin'] = sync_msg
		fields['action'] = _suggest(detached, row['staged'], row['unstaged'], kind)
		fields['ready'] = 'yes' if ready else 'NO'
		if ready:
			push_commands.append(_suggested_push_command(repo, branch, ready))
		all_ready = all_ready and ready
		ctx.printer.put(repo.name, **fields)

	if all_ready and push_commands:
		ctx.printer.put(
			'suggested_push_command',
			command='\n'.join(push_commands),
		)
	else:
		ctx.printer.put('suggested_push_command', command='none')

	return 0 if all_ready else 1
