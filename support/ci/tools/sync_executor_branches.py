#!/usr/bin/env python3
"""
Project manager branch gitlinks onto the executor repository branches.

Manager release branches are the source of truth. A push to `v<X>.<Y>` moves
each active executor line's branch declared in `.gitmodules`; a push to
`v<X>.<Y>-dev` moves the corresponding `<line>-dev` branch. Updates are plain
Git pushes, so a missing branch is created and an existing branch moves only by
fast-forward.

Every executor line is attempted even when another line fails. Failures are
reported together after the last line, leaving a rerun to finish only the refs
that still need work.
"""

import argparse
import os
import re
import subprocess

import ci_lib
import gh_common

from tools.versions import active_lines

MANAGER_BRANCH_RE = re.compile(r'^v\d+\.\d+(?P<dev>-dev)?$')


class SyncError(RuntimeError):
	pass


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
	return ci_lib.run(['git', *args], check=check, capture_output=True)


def target_branch(manager_branch: str, declared_branch: str) -> str:
	match = MANAGER_BRANCH_RE.fullmatch(manager_branch)
	if match is None:
		raise SyncError(
			f'unsupported manager branch `{manager_branch}`; expected v<X>.<Y> or '
			'v<X>.<Y>-dev'
		)
	if not match.group('dev'):
		return declared_branch
	if not declared_branch.endswith('.x'):
		raise SyncError(
			f'declared executor branch `{declared_branch}` does not end in `.x`; '
			'cannot derive its dev branch'
		)
	return f'{declared_branch[:-2]}-dev'


def declared_branch(path: str) -> str:
	result = git(
		'config',
		'-f',
		'.gitmodules',
		'--get',
		f'submodule.{path}.branch',
		check=False,
	)
	branch = result.stdout.strip()
	if result.returncode != 0 or not branch:
		raise SyncError(f'no branch declared for `{path}` in .gitmodules')
	return branch


def sync_line(line: str, manager_branch: str) -> tuple[str, str]:
	path = f'executors/{line}.x'
	target = target_branch(manager_branch, declared_branch(path))

	initialized = git(
		'submodule', 'update', '--init', '--depth', '1', '--', path, check=False
	)
	if initialized.returncode != 0:
		detail = (initialized.stderr or initialized.stdout).strip()
		raise SyncError(
			f'could not materialize `{path}`'
			+ (f': {detail}' if detail else f' (git exited {initialized.returncode})')
		)

	resolved = git('-C', path, 'rev-parse', 'HEAD', check=False)
	sha = resolved.stdout.strip()
	if resolved.returncode != 0 or not sha:
		detail = (resolved.stderr or resolved.stdout).strip()
		raise SyncError(
			f'could not resolve the gitlink checkout for `{path}`'
			+ (f': {detail}' if detail else '')
		)
	result = git(
		'-C',
		path,
		'push',
		'origin',
		f'{sha}:refs/heads/{target}',
		check=False,
	)
	if result.returncode != 0:
		detail = (result.stderr or result.stdout).strip()
		raise SyncError(
			f'could not create or fast-forward `{target}` to `{sha}`'
			+ (f': {detail}' if detail else '')
		)
	return target, sha


def sync_executor_branches(manager_branch: str) -> int:
	if MANAGER_BRANCH_RE.fullmatch(manager_branch) is None:
		ci_lib.github_error(
			f'unsupported manager branch `{manager_branch}`; expected v<X>.<Y> or '
			'v<X>.<Y>-dev'
		)
		return 1

	errors: list[tuple[str, str]] = []
	for line in active_lines():
		try:
			target, sha = sync_line(line, manager_branch)
			print(f'{line}: `{target}` -> `{sha}`')
		except SyncError as error:
			message = str(error)
			ci_lib.github_error(f'{line}: {message}')
			errors.append((line, message))

	if errors:
		print(f'\n{len(errors)} executor branch update(s) failed:')
		for line, message in errors:
			print(f'- {line}: {message}')
		return 1
	print('all executor branches are synchronized')
	return 0


class SyncExecutorBranches(ci_lib.Tool):
	"""
	Create or fast-forward executor branches from a manager branch tip.
	"""

	def name(self) -> str:
		return 'sync-executor-branches'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		gh_common.add_args(parser, executor_repo=False, pr=False, head_ref=False)
		parser.add_argument(
			'--manager-branch',
			default=None,
			help='manager branch (default: $MANAGER_BRANCH or $GITHUB_REF_NAME)',
		)

	def handler(self, args: argparse.Namespace) -> int:
		branch = (
			args.manager_branch
			or os.environ.get('MANAGER_BRANCH')
			or os.environ.get('GITHUB_REF_NAME', '')
		)
		if not branch:
			ci_lib.github_error(
				'manager branch is required: pass --manager-branch or set $MANAGER_BRANCH'
			)
			return 2
		return sync_executor_branches(branch)


COMMANDS = [SyncExecutorBranches()]
