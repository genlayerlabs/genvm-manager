"""Verify that an outgoing manager commit pins published executor commits.

The manager ref defaults to ``PRE_COMMIT_TO_REF`` in a pre-push hook and to
``HEAD`` for manual runs. The active executor set and gitlinks are read from
that commit, never from the working tree. Remote queries are read-only,
non-interactive, and bounded to 20 seconds.

Publication is a question about a commit, not about a ref: a gitlink counts as
published when the remote holds that object, whatever branch carries it and
whether or not it is a branch tip. ``fetch --negotiate-only`` asks exactly
that -- it reports which of our commits the remote already has and transfers
nothing -- so a stale, shallow or single-branch local clone cannot skew the
answer. Listing refs cannot: ``ls-remote`` matches ref names, so it can only
recognize a gitlink that happens to be a tip right now.

Only a proven unpublished gitlink is rejected. Missing local state, unreachable
remotes and timeouts warn but allow the push.
"""

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .. import common

NAME = 'verify-pushed-executors'
HELP = 'reject manager refs that pin executor commits absent from their origin'


@dataclass(frozen=True)
class _Target:
	rel: str
	path: Path
	sha: str


def configure(parser):
	parser.add_argument(
		'--manager-ref',
		help='manager commit to verify (default: PRE_COMMIT_TO_REF, then HEAD)',
	)
	parser.add_argument(
		'--executor-remote',
		default='origin',
		help='executor remote name (default: origin)',
	)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
	return common.git(root, *args)


def _failure(ctx: common.Context, rel: str, sha: str, problem: str) -> None:
	ctx.printer.put(
		'verify-pushed-executors',
		executor=rel,
		gitlink=sha[:12] if sha else 'unknown',
		status='REJECTED',
		problem=problem,
	)


def _warning(ctx: common.Context, rel: str, sha: str, problem: str) -> None:
	ctx.printer.put(
		'verify-pushed-executors',
		executor=rel,
		gitlink=sha[:12] if sha else 'unknown',
		status='WARNING (push allowed)',
		problem=problem,
	)


def _recovery(ctx: common.Context) -> None:
	ctx.printer.put(
		'recovery',
		command=(
			'genvm-tool git check-for-push\n'
			'git -C executors/<line>.x push origin <executor-branch>\n'
			'git push origin <manager-branch>'
		),
	)


def _resolve_manager_commit(root: Path, manager_ref: str) -> tuple[str, str | None]:
	result = _git(
		root,
		'rev-parse',
		'--verify',
		'--end-of-options',
		f'{manager_ref}^{{commit}}',
	)
	if result.returncode:
		return '', f'cannot resolve manager ref {manager_ref!r} to a commit'
	return result.stdout.strip(), None


def _targets_from_commit(
	root: Path, commit: str, remote: str
) -> tuple[
	list[_Target],
	list[tuple[str, str, str]],
	list[tuple[str, str, str]],
]:
	failures: list[tuple[str, str, str]] = []
	warnings: list[tuple[str, str, str]] = []
	shown = _git(root, 'show', f'{commit}:{common.MONOREPO_MARKER}')
	if shown.returncode:
		failures.append(
			(
				common.MONOREPO_MARKER,
				'',
				f'cannot read {common.MONOREPO_MARKER} from outgoing manager commit',
			)
		)
		return [], failures, warnings
	try:
		config = json.loads(shown.stdout)
		versions = common.active_versions_from_config(config)
	except (AttributeError, json.JSONDecodeError, TypeError, ValueError):
		failures.append(
			(
				common.MONOREPO_MARKER,
				'',
				f'malformed {common.MONOREPO_MARKER} in outgoing manager commit',
			)
		)
		return [], failures, warnings

	targets = []
	for version in versions:
		rel = common.executor_rel(version)
		entry = _git(root, 'ls-tree', commit, '--', rel)
		fields = entry.stdout.strip().split(maxsplit=3)
		if (
			entry.returncode
			or len(fields) != 4
			or fields[0] != '160000'
			or fields[1] != 'commit'
			or fields[3] != rel
		):
			failures.append((rel, '', 'missing or malformed gitlink tree entry'))
			continue
		sha = fields[2]
		path = root / rel
		if not (path / '.git').exists():
			warnings.append((rel, sha, 'executor checkout is missing; not verified'))
			continue
		if not common.remote_url(path, remote):
			warnings.append(
				(rel, sha, f'executor remote {remote!r} is missing; not verified')
			)
			continue
		targets.append(_Target(rel, path, sha))
	return targets, failures, warnings


def _published(target: _Target, remote: str) -> tuple[bool | None, str]:
	"""Whether ``remote`` already holds the gitlink commit.

	``--negotiate-only`` runs the fetch negotiation and stops before any
	transfer, printing the commits both sides have. Our own sha comes back only
	if the remote has that object, which is the property we care about; no ref
	has to point at it and no part of the remote history has to be local.
	"""
	if _git(target.path, 'cat-file', '-e', f'{target.sha}^{{commit}}').returncode:
		return None, 'gitlink is absent from this executor checkout; not verified'
	try:
		result = subprocess.run(
			[
				'git',
				'-C',
				str(target.path),
				'-c',
				'protocol.version=2',
				'fetch',
				'--negotiate-only',
				f'--negotiation-tip={target.sha}',
				remote,
			],
			capture_output=True,
			text=True,
			timeout=common.GIT_REMOTE_TIMEOUT,
			env=common.git_env(**common.GIT_NETWORK_ENV),
		)
	except subprocess.TimeoutExpired:
		return None, f'executor remote {remote!r} timed out'
	if result.returncode:
		if 'negotiate-only' in result.stderr:
			return None, 'git is too old for --negotiate-only; not verified'
		return None, f'executor remote {remote!r} is unreachable'
	if target.sha in result.stdout.split():
		return True, ''
	return False, f'gitlink is absent from executor remote {remote!r}'


def main(ctx: common.Context, args) -> int:
	manager_ref = args.manager_ref or os.environ.get('PRE_COMMIT_TO_REF') or 'HEAD'
	commit, problem = _resolve_manager_commit(ctx.root, manager_ref)
	if problem:
		_failure(ctx, 'manager', '', problem)
		_recovery(ctx)
		return 1

	targets, failures, warnings = _targets_from_commit(
		ctx.root, commit, args.executor_remote
	)
	# Asked per executor, not per remote URL: the lines share one repository but
	# not one object store, so each question must be put from the clone that
	# holds the commit being asked about.
	for target in targets:
		ok, problem = _published(target, args.executor_remote)
		if ok is False:
			failures.append((target.rel, target.sha, problem))
		elif ok is None:
			warnings.append((target.rel, target.sha, problem))

	for rel, sha, warning in warnings:
		_warning(ctx, rel, sha, warning)

	if failures:
		for rel, sha, failure in failures:
			_failure(ctx, rel, sha, failure)
		_recovery(ctx)
		return 1

	ctx.printer.put(
		'verify-pushed-executors',
		manager_ref=manager_ref,
		manager_commit=commit[:12],
		status='ok-with-warnings' if warnings else 'ok',
		warnings=len(warnings),
	)
	return 0
