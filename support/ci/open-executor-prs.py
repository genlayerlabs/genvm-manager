#!/usr/bin/env python3
"""Open (or reuse) the linked executor PR for a manager PR and list it on the PR.

Invoked by .github/workflows/branch_executor_prs.yaml when a manager PR is opened
against a v<X>-dev branch. The manager's executor-submodule work lives on a mirror
branch in the shared genvm-executor repo, namespaced `pr/<line>/<feature>` so the
two active lines never collide (see genvm_tool.common.Repo.feature_branch). This:

	1. derives the line from the manager PR base (`v0.3-dev` -> `v0.3`) and the
		mirror branch `pr/<line>/<head>`;
	2. if that branch exists on genvm-executor, opens a PR `pr/<line>/<head>` ->
		`v<X>-dev` there (reusing an already-open one — idempotent on re-runs);
	3. upserts a marked comment on the manager PR listing it as an `executor: <url>`
		line.

Two tokens: the executor PR is created with EXECUTOR_TOKEN (a PAT with
genvm-executor access — the default GITHUB_TOKEN is manager-scoped and cannot
touch another repo); the manager comment uses MANAGER_TOKEN (the default
GITHUB_TOKEN). Never executes PR code — only `gh` API/PR calls.

Env: MANAGER_REPO, EXECUTOR_REPO, PR_NUMBER, HEAD_REF, BASE_REF, MANAGER_TOKEN,
EXECUTOR_TOKEN.
"""

import json
import os
import re
import subprocess

MANAGER_REPO = os.environ['MANAGER_REPO']
EXECUTOR_REPO = os.environ.get('EXECUTOR_REPO', 'genlayerlabs/genvm-executor')
PR = os.environ['PR_NUMBER']
HEAD_REF = os.environ['HEAD_REF']
BASE_REF = os.environ['BASE_REF']
MANAGER_TOKEN = os.environ['MANAGER_TOKEN']
EXECUTOR_TOKEN = os.environ['EXECUTOR_TOKEN']

# Marker so re-runs update the same comment instead of stacking new ones.
COMMENT_MARKER = '<!-- genvm-executor-prs -->'


def gh(*args, token, check=True):
	"""`gh` with GH_TOKEN bound to the given token (manager- or executor-scoped)."""
	return subprocess.run(
		['gh', *args],
		env={**os.environ, 'GH_TOKEN': token},
		check=check,
		text=True,
		capture_output=True,
	)


def line_of(base: str) -> str | None:
	"""Version line for a dev base branch: `v0.3-dev` -> `v0.3`, else None."""
	m = re.fullmatch(r'(v\d+\.\d+)-dev', base)
	return m.group(1) if m else None


def executor_branch_exists(branch: str) -> bool:
	# matching-refs returns [] (not a 404) when nothing matches; it is a prefix
	# query, so confirm an EXACT ref before treating the branch as present.
	out = gh(
		'api',
		f'repos/{EXECUTOR_REPO}/git/matching-refs/heads/{branch}',
		token=EXECUTOR_TOKEN,
	).stdout
	return any(r.get('ref') == f'refs/heads/{branch}' for r in json.loads(out or '[]'))


def existing_pr(head: str, base: str) -> str | None:
	url = gh(
		'pr',
		'list',
		'--repo',
		EXECUTOR_REPO,
		'--head',
		head,
		'--base',
		base,
		'--state',
		'open',
		'--json',
		'url',
		'--jq',
		'.[0].url // ""',
		token=EXECUTOR_TOKEN,
	).stdout.strip()
	return url or None


def open_pr(head: str, base: str) -> str:
	title = gh(
		'pr',
		'view',
		PR,
		'--repo',
		MANAGER_REPO,
		'--json',
		'title',
		'--jq',
		'.title',
		token=MANAGER_TOKEN,
	).stdout.strip()
	body = (
		f'Auto-opened executor mirror of {MANAGER_REPO}#{PR}.\n\n'
		f'Carries the executor-side work for that manager PR. Closed and its branch '
		f'`{head}` deleted automatically when the manager PR merges into `{base}`.'
	)
	r = gh(
		'pr',
		'create',
		'--repo',
		EXECUTOR_REPO,
		'--base',
		base,
		'--head',
		head,
		'--title',
		title,
		'--body',
		body,
		token=EXECUTOR_TOKEN,
		check=False,
	)
	if r.returncode != 0:
		# Lost a race against a concurrent run? Fall back to whatever is open now.
		url = existing_pr(head, base)
		if url:
			return url
		raise SystemExit(f'failed to create executor PR: {r.stderr.strip()}')
	return r.stdout.strip().splitlines()[-1].strip()


def upsert_comment(lines: list[str]) -> None:
	body = f'{COMMENT_MARKER}\n### Linked executor PR(s)\n\n' + '\n'.join(lines) + '\n'
	ids = gh(
		'api',
		f'repos/{MANAGER_REPO}/issues/{PR}/comments',
		'--jq',
		f'.[] | select(.body | contains("{COMMENT_MARKER}")) | .id',
		token=MANAGER_TOKEN,
	).stdout.split()
	if ids:
		gh(
			'api',
			'--method',
			'PATCH',
			f'repos/{MANAGER_REPO}/issues/comments/{ids[0]}',
			'-f',
			f'body={body}',
			token=MANAGER_TOKEN,
		)
	else:
		gh(
			'api',
			'--method',
			'POST',
			f'repos/{MANAGER_REPO}/issues/{PR}/comments',
			'-f',
			f'body={body}',
			token=MANAGER_TOKEN,
		)


def main() -> None:
	line = line_of(BASE_REF)
	if not line:
		print(f'base `{BASE_REF}` is not a v<X>-dev branch; nothing to do')
		return
	head = f'pr/{line}/{HEAD_REF}'
	base = BASE_REF  # the executor's dev branch carries the same name
	if not executor_branch_exists(head):
		print(
			f'executor branch `{head}` is not on {EXECUTOR_REPO} yet; nothing to open '
			f'(push the executor submodule branch first, then reopen the PR)'
		)
		return
	url = existing_pr(head, base) or open_pr(head, base)
	print(f'executor PR: {url}')
	upsert_comment([f'executor: {url}'])


if __name__ == '__main__':
	main()
