#!/usr/bin/env python3
"""Open (or reuse) the linked executor PRs for a manager PR and list them on it.

Invoked by .github/workflows/branch_executor_prs.yaml when a manager PR is opened
against a dev branch. The manager release line (e.g. v0.6) is independent of the
executor lines it ships (v0.2, v0.3): one manager PR bundles the work of EVERY
active executor line, each on its own line-namespaced mirror branch in the shared
genvm-executor repo (`pr/<line>/<feature>`, see genvm_tool.common.Repo.feature_branch).

So we fan out over the active executor lines (.genvm-monorepo-root via
branch-versions.py) rather than deriving a single line from the manager base. For
each active line whose mirror branch `pr/<line>/<head>` exists on genvm-executor we
open (or reuse) a PR `pr/<line>/<head>` -> `<line>-dev` there, then upsert a marked
comment on the manager PR listing each as an `executor: <url>` line.

Two tokens: the executor PRs are created with EXECUTOR_TOKEN (a PAT with
genvm-executor access — the default GITHUB_TOKEN is manager-scoped and cannot touch
another repo); the manager comment uses MANAGER_TOKEN (the default GITHUB_TOKEN).
Never executes PR code — only `gh` API/PR calls.

Env: MANAGER_REPO, EXECUTOR_REPO, PR_NUMBER, HEAD_REF, MANAGER_TOKEN, EXECUTOR_TOKEN.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

MANAGER_REPO = os.environ['MANAGER_REPO']
EXECUTOR_REPO = os.environ.get('EXECUTOR_REPO', 'genlayerlabs/genvm-executor')
PR = os.environ['PR_NUMBER']
HEAD_REF = os.environ['HEAD_REF']
MANAGER_TOKEN = os.environ['MANAGER_TOKEN']
EXECUTOR_TOKEN = os.environ['EXECUTOR_TOKEN']

HERE = Path(__file__).resolve().parent
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


def active_lines() -> list[str]:
	"""Active executor lines as `v<X>` tags (e.g. ['v0.2', 'v0.3'])."""
	out = subprocess.run(
		[sys.executable, str(HERE / 'branch-versions.py'), 'list'],
		check=True,
		text=True,
		capture_output=True,
	).stdout
	return [f'v{v}' for v in out.split()]


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
		f'`{head}` deleted automatically when the manager PR merges.'
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
		raise SystemExit(
			f'failed to create executor PR `{head}` -> `{base}`: {r.stderr.strip()}'
		)
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
	lines = []
	for line in active_lines():
		head = f'pr/{line}/{HEAD_REF}'
		base = f'{line}-dev'
		if not executor_branch_exists(head):
			print(f'no executor branch `{head}`; skipping {line} (nothing pushed for it)')
			continue
		url = existing_pr(head, base) or open_pr(head, base)
		print(f'{line}: executor PR {url}')
		lines.append(f'executor: {url}')
	if lines:
		upsert_comment(lines)
	else:
		print('no executor mirror branches found for any active line; nothing to link')


if __name__ == '__main__':
	main()
