#!/usr/bin/env python3
"""Open (or reuse) the linked executor PRs for a manager PR and list them on it.

Invoked by .github/workflows/branch_executor_prs.yaml when a manager PR is opened
against a dev branch or its head is pushed (`synchronize`), so mirror branches
pushed after the PR opens still get linked. The manager release line (e.g. v0.6)
is independent of the
executor lines it ships (v0.2, v0.3): one manager PR bundles the work of EVERY
active executor line, each on its own line-namespaced mirror branch in the shared
genvm-executor repo (`pr/<line>/<feature>`, see genvm_tool.common.Repo.feature_branch).

So we fan out over the active executor lines (.genvm-monorepo-root via
tools.versions) rather than deriving a single line from the manager base. For
each active line whose mirror branch `pr/<line>/<head>` exists on genvm-executor we
open (or reuse) a PR `pr/<line>/<head>` -> `<line>-dev` there, then upsert a marked
comment on the manager PR listing each as an `executor: <url>` line.

Two tokens: the executor PRs are created with the executor token (a PAT with
genvm-executor access — the default GITHUB_TOKEN is manager-scoped and cannot touch
another repo); the manager comment uses the manager token. Both are optional and
fall back to ambient `gh` auth for local runs. Never executes PR code — only `gh`
API/PR calls.

Inputs (repos, PR number, head ref) resolve arg > env > default via gh_common.
"""

import argparse
import json

import ci_lib
import gh_common
from gh_common import gh

from tools.versions import active_lines

# Marker so re-runs update the same comment instead of stacking new ones.
COMMENT_MARKER = '<!-- genvm-executor-prs -->'


def executor_branch_exists(ctx: gh_common.Ctx, branch: str) -> bool:
	# matching-refs returns [] (not a 404) when nothing matches; it is a prefix
	# query, so confirm an EXACT ref before treating the branch as present.
	out = gh(
		'api',
		f'repos/{ctx.executor_repo}/git/matching-refs/heads/{branch}',
		token=gh_common.executor_token(),
	).stdout
	return any(r.get('ref') == f'refs/heads/{branch}' for r in json.loads(out or '[]'))


def existing_pr(ctx: gh_common.Ctx, head: str, base: str) -> str | None:
	url = gh(
		'pr',
		'list',
		'--repo',
		ctx.executor_repo,
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
		token=gh_common.executor_token(),
	).stdout.strip()
	return url or None


def open_pr(ctx: gh_common.Ctx, head: str, base: str) -> str:
	title = gh(
		'pr',
		'view',
		ctx.pr_number,
		'--repo',
		ctx.manager_repo,
		'--json',
		'title',
		'--jq',
		'.title',
		token=gh_common.manager_token(),
	).stdout.strip()
	body = (
		f'Auto-opened executor mirror of {ctx.manager_repo}#{ctx.pr_number}.\n\n'
		f'Carries the executor-side work for that manager PR. Auto-closed as merged '
		f'when the manager PR lands (its `{head}` branch is moved onto `{base}`).'
	)
	r = gh(
		'pr',
		'create',
		'--repo',
		ctx.executor_repo,
		'--base',
		base,
		'--head',
		head,
		'--title',
		title,
		'--body',
		body,
		token=gh_common.executor_token(),
		check=False,
	)
	if r.returncode != 0:
		# Lost a race against a concurrent run? Fall back to whatever is open now.
		url = existing_pr(ctx, head, base)
		if url:
			return url
		raise SystemExit(
			f'failed to create executor PR `{head}` -> `{base}`: {r.stderr.strip()}'
		)
	return r.stdout.strip().splitlines()[-1].strip()


def upsert_comment(ctx: gh_common.Ctx, lines: list[str]) -> None:
	body = f'{COMMENT_MARKER}\n### Linked executor PR(s)\n\n' + '\n'.join(lines) + '\n'
	ids = gh(
		'api',
		f'repos/{ctx.manager_repo}/issues/{ctx.pr_number}/comments',
		'--jq',
		f'.[] | select(.body | contains("{COMMENT_MARKER}")) | .id',
		token=gh_common.manager_token(),
	).stdout.split()
	if ids:
		gh(
			'api',
			'--method',
			'PATCH',
			f'repos/{ctx.manager_repo}/issues/comments/{ids[0]}',
			'-f',
			f'body={body}',
			token=gh_common.manager_token(),
		)
	else:
		gh(
			'api',
			'--method',
			'POST',
			f'repos/{ctx.manager_repo}/issues/{ctx.pr_number}/comments',
			'-f',
			f'body={body}',
			token=gh_common.manager_token(),
		)


class OpenExecutorPrs(ci_lib.Tool):
	"""Open (or reuse) the linked executor PRs for a manager PR and list them on it."""

	def name(self) -> str:
		return 'open-executor-prs'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		gh_common.add_args(parser)

	def handler(self, args: argparse.Namespace) -> int:
		open_executor_prs(gh_common.Ctx.from_args(args))
		return 0


def open_executor_prs(ctx: gh_common.Ctx) -> None:
	head_ref = ctx.head_ref  # resolve once (may shell out to git locally)
	lines = []
	for line in active_lines():
		head = f'pr/{line}/{head_ref}'
		base = f'{line}-dev'
		if not executor_branch_exists(ctx, head):
			print(f'no executor branch `{head}`; skipping {line} (nothing pushed for it)')
			continue
		url = existing_pr(ctx, head, base) or open_pr(ctx, head, base)
		print(f'{line}: executor PR {url}')
		lines.append(f'executor: {url}')
	if lines:
		upsert_comment(ctx, lines)
	else:
		print('no executor mirror branches found for any active line; nothing to link')


COMMANDS = [OpenExecutorPrs()]
