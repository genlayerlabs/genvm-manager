#!/usr/bin/env python3
"""
Render and reconcile the manager linkage carried by an executor mirror PR.

Cross-repo E2E resolves a GenVM manager PR and consumes executor code through
the gitlinks committed there. An executor PR is therefore a review/landing
surface, not an independent E2E input. Every executor mirror PR gets one managed
body section that says this visibly and exposes the relationship as JSON for
automation.
"""

import json
import re
from collections.abc import Callable
from urllib.parse import urlparse

import gh_common

LINK_START = '<!-- genvm-manager-link'
LINK_END = '<!-- /genvm-manager-link -->'
LINK_RE = re.compile(
	rf'{re.escape(LINK_START)}\n.*?\n-->\n.*?{re.escape(LINK_END)}',
	re.DOTALL,
)


def manager_pr_url(manager_repo: str, manager_pr: str) -> str:
	return f'https://github.com/{manager_repo}/pull/{manager_pr}'


def metadata(
	manager_repo: str,
	manager_pr: str,
	line: str,
	head: str,
	base: str,
) -> dict:
	"""Machine-readable ownership metadata embedded in the executor PR body."""
	return {
		'schema_version': 1,
		'manager_repo': manager_repo,
		'manager_pr': int(manager_pr),
		'manager_url': manager_pr_url(manager_repo, manager_pr),
		'executor_line': line,
		'gitlink_path': f'executors/{line}.x',
		'executor_head': head,
		'executor_base': base,
		'cross_repo_e2e_source': 'manager-pr-gitlink',
	}


def render(
	manager_repo: str,
	manager_pr: str,
	line: str,
	head: str,
	base: str,
) -> str:
	"""The complete managed section for an executor mirror PR body."""
	data = metadata(manager_repo, manager_pr, line, head, base)
	encoded = json.dumps(data, sort_keys=True, separators=(',', ':'))
	url = data['manager_url']
	path = data['gitlink_path']
	return (
		f'{LINK_START}\n{encoded}\n-->\n'
		f'### Manager and cross-repo E2E\n\n'
		f'- **Manager PR:** [{manager_repo}#{manager_pr}]({url})\n'
		f'- **Executor line:** `{line}` via manager gitlink `{path}`\n'
		f'- **E2E ownership:** the manager PR above is the cross-repo E2E input. '
		f'This executor PR alone is not enrolled in cross-repo E2E; its changes are '
		f'covered only when that manager PR pins them through `{path}`.\n\n'
		f'This mirror lands with the manager PR by moving `{head}` onto `{base}`; '
		f'do not merge it independently.\n'
		f'{LINK_END}'
	)


def upsert(body: str, section: str) -> str:
	"""Insert or replace the managed section without changing unrelated text."""
	if LINK_RE.search(body):
		return LINK_RE.sub(section, body, count=1)
	body = body.rstrip()
	return f'{body}\n\n{section}' if body else section


def pr_number_from_url(pr_url: str) -> str:
	"""Pull request number from the canonical URL returned by `gh pr create`."""
	path = urlparse(pr_url).path.rstrip('/').split('/')
	if len(path) < 4 or path[-2] != 'pull' or not path[-1].isdigit():
		raise ValueError(f'not a canonical GitHub pull request URL: {pr_url}')
	return path[-1]


def reconcile(
	*,
	executor_repo: str,
	executor_pr_url: str,
	manager_repo: str,
	manager_pr: str,
	line: str,
	head: str,
	base: str,
	token: str | None,
	gh: Callable = gh_common.gh,
) -> None:
	"""Upsert the managed linkage section on a new or existing executor PR."""
	pr_number = pr_number_from_url(executor_pr_url)
	current = gh(
		'api',
		f'repos/{executor_repo}/pulls/{pr_number}',
		'--jq',
		'.body // ""',
		token=token,
	).stdout
	updated = upsert(current, render(manager_repo, manager_pr, line, head, base))
	if updated == current:
		return
	gh(
		'api',
		'--method',
		'PATCH',
		f'repos/{executor_repo}/pulls/{pr_number}',
		'-f',
		f'body={updated}',
		token=token,
		retry=False,
	)
