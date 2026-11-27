#!/usr/bin/env python3
"""
Report what each repo changed in this CI run, as JSON.

Emits one entry per repo: the manager keyed as ``.``, every executor submodule by
its path, each holding ``base_commit``, ``branch_commit`` and ``has_changes``.

A submodule's commits are not visible from the manager's history, so its
endpoints are the gitlinks recorded on either side of the pull request.

Outside a pull request there is nothing to compare against, so ``base_commit ==
branch_commit`` and ``has_changes`` is false everywhere.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

MANAGER = '.'

BASE_REF = 'refs/genvm-ci/base'
HEAD_REF = 'refs/genvm-ci/prhead'


def git(*args: str, check: bool = True) -> str:
	proc = subprocess.run(
		['git', *args],
		check=check,
		capture_output=True,
		text=True,
	)
	return proc.stdout.strip()


def submodule_paths() -> list[str]:
	if not Path('.gitmodules').exists():
		return []
	out = git('config', '--file', '.gitmodules', '--get-regexp', r'^submodule\..*\.path$')
	return sorted(line.split(' ', 1)[1] for line in out.splitlines() if line)


def gitlink_at(commit: str, path: str) -> str | None:
	"""The submodule commit `path` points at, as recorded in `commit`'s tree."""
	entry = git('ls-tree', commit, path)
	if not entry:
		return None
	meta, _, _name = entry.partition('\t')
	mode, obj_type, sha = meta.split()
	if obj_type != 'commit':
		return None
	return sha


def endpoints(base: str, head: str, path: str) -> tuple[str | None, str | None]:
	if path == MANAGER:
		return base, head
	return gitlink_at(base, path), gitlink_at(head, path)


def fetch_pr_refs(base_ref: str, pr_number: str) -> tuple[str, str]:
	"""
	Materialize the base tip and the PR head, and return (merge_base, head).

	A `pull_request` checkout leaves HEAD on the merge commit, and the base
	branch is usually not fetched at all, so both sides are fetched by explicit
	refspec (which also works for forks, where the head sha is not fetchable on
	its own).

	`--no-recurse-submodules` is required: we only read gitlink SHAs out of the
	superproject trees (`git ls-tree`), never any submodule object. Once the
	submodules are populated (get-all-git.py runs before us), git's default
	`fetch.recurseSubmodules=on-demand` would otherwise try to fetch every
	gitlink the newly-fetched PR range touches — and a historical commit in the
	range may pin an executor commit that was never pushed (or since deleted)
	on its remote, which aborts the whole fetch with `upload-pack: not our ref`.
	"""
	git(
		'fetch',
		'--no-tags',
		'--no-recurse-submodules',
		'origin',
		f'+refs/heads/{base_ref}:{BASE_REF}',
		f'+refs/pull/{pr_number}/head:{HEAD_REF}',
	)
	head = git('rev-parse', HEAD_REF)
	# The repo requires PR branches to be 0 commits behind, so this is normally
	# the base tip; taking the merge base anyway keeps the range honest if that
	# gate is ever relaxed.
	merge_base = git('merge-base', BASE_REF, HEAD_REF)
	return merge_base, head


def gh(*args: str) -> str:
	proc = subprocess.run(['gh', *args], check=True, capture_output=True, text=True)
	return proc.stdout.strip()


def base_ref_of(pr_number: str) -> str:
	"""
	The PR's base branch, read from the API.

	`GITHUB_BASE_REF` is only set on `pull_request` events, but these checks also
	run on the panel's `workflow_dispatch`, where the PR number is known and the
	event payload is not. Falling back to "not a pull request" there would report
	no changes at all — a silent pass.
	"""
	repo = os.environ.get('GITHUB_REPOSITORY', '')
	return gh('api', f'repos/{repo}/pulls/{pr_number}', '--jq', '.base.ref')


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		'--github-output',
		action='store_true',
		help='also append `changes=<json>` to $GITHUB_OUTPUT',
	)
	args = parser.parse_args()

	base_ref = os.environ.get('GITHUB_BASE_REF', '')
	pr_number = os.environ.get('GITHUB_PR_NUMBER', '')

	paths = [MANAGER, *submodule_paths()]

	# Keyed off the PR NUMBER, not the event name: the number is available on
	# every event that validates a PR, and keying off `pull_request` made this
	# silently report "no changes" on panel-dispatched runs.
	if pr_number:
		if not base_ref:
			base_ref = base_ref_of(pr_number)
		base, head = fetch_pr_refs(base_ref, pr_number)
	else:
		# Not a pull request: nothing to diff against.
		base = head = git('rev-parse', 'HEAD')

	result = {}
	for path in paths:
		base_commit, branch_commit = endpoints(base, head, path)
		result[path] = {
			'base_commit': base_commit,
			'branch_commit': branch_commit,
			# A submodule that only exists on one side (added or removed) counts
			# as changed; one that exists on neither does not.
			'has_changes': base_commit != branch_commit
			and not (base_commit is None and branch_commit is None),
		}

	encoded = json.dumps(result)
	print(encoded)

	if args.github_output:
		github_output = os.environ.get('GITHUB_OUTPUT')
		if not github_output:
			print(
				'::error::--github-output given but GITHUB_OUTPUT is unset', file=sys.stderr
			)
			return 1
		with open(github_output, 'a', encoding='utf-8') as out:
			out.write(f'changes={encoded}\n')

	return 0


if __name__ == '__main__':
	try:
		sys.exit(main())
	except subprocess.CalledProcessError as e:
		import shlex
		import traceback

		traceback.print_exc()
		print(' '.join(shlex.quote(a) for a in e.cmd), file=sys.stderr)
		print(e.stdout, file=sys.stdout)
		print(e.stderr, file=sys.stderr)
		sys.exit(1)
