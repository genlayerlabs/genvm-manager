#!/usr/bin/env python3
"""Shared GitHub context for CI tools: repos, PR number, head ref, tokens.

Almost every GitHub-touching tool needs the same handful of inputs, resolved the
same way — **CLI arg > environment variable > default**. This module is the one
place that precedence lives:

- `add_args(parser, ...)` registers the common flags (`--manager-repo`,
	`--executor-repo`, `--pr`, `--head-ref`). They default to `None`; the arg
	simply wins when passed.
- `Ctx.from_args(args)` folds arg → env → default into resolved values. A tool
	reads `ctx.manager_repo`, `ctx.pr_number`, etc. regardless of which flags it
	registered — an unregistered field is `None` on the namespace and falls
	through to env/default just the same.

Tokens are env-only (never a CLI flag) and optional: `manager_token()` /
`executor_token()` return `None` when unset, and `gh(..., token=None)` then runs
with ambient `gh` auth (a local `gh auth login`). That is what lets these tools
run outside CI without minting a PAT — the whole point of not hardcoding tokens.
"""

import argparse
import dataclasses
import os
import subprocess
import sys
import time

MANAGER_REPO_DEFAULT = 'genlayerlabs/genvm-manager'
EXECUTOR_REPO_DEFAULT = 'genlayerlabs/genvm-executor'


def _env(*names: str) -> str | None:
	"""First set, non-empty value among env `names`, else None."""
	for name in names:
		val = os.environ.get(name)
		if val:
			return val
	return None


def manager_token() -> str | None:
	"""Manager-repo token, or None for ambient `gh` auth. `MANAGER_TOKEN` is the
	CI name; `GH_TOKEN` is accepted so a single ambient token also works."""
	return _env('MANAGER_TOKEN', 'GH_TOKEN')


def executor_token() -> str | None:
	"""Executor-repo token, or None for ambient `gh` auth (`EXECUTOR_TOKEN`)."""
	return _env('EXECUTOR_TOKEN')


def current_branch() -> str:
	"""Current git branch — the local fallback for a PR head ref."""
	return subprocess.run(
		['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
		check=True,
		text=True,
		capture_output=True,
	).stdout.strip()


def add_args(
	parser: argparse.ArgumentParser,
	*,
	executor_repo: bool = True,
	pr: bool = True,
	head_ref: bool = True,
) -> None:
	"""Register the common flags on `parser`. `--manager-repo` is always added;
	the rest are opt-out for the few tools that don't need them. Each defaults to
	None so `Ctx.from_args` can apply arg > env > default uniformly."""
	parser.add_argument(
		'--manager-repo',
		default=None,
		help='manager repo slug (default: $MANAGER_REPO / $GITHUB_REPOSITORY / '
		f'{MANAGER_REPO_DEFAULT})',
	)
	if executor_repo:
		parser.add_argument(
			'--executor-repo',
			default=None,
			help=f'executor repo slug (default: $EXECUTOR_REPO / {EXECUTOR_REPO_DEFAULT})',
		)
	if pr:
		parser.add_argument(
			'--pr',
			dest='pr_number',
			default=None,
			help='manager PR number (default: $PR_NUMBER)',
		)
	if head_ref:
		parser.add_argument(
			'--head-ref',
			default=None,
			help='manager PR head branch '
			'(default: $HEAD_REF / $GITHUB_HEAD_REF, else the current branch)',
		)


@dataclasses.dataclass(frozen=True)
class Ctx:
	"""Resolved GitHub inputs for a tool run (arg > env > default applied)."""

	manager_repo: str
	executor_repo: str
	_pr_number: str | None
	_head_ref: str | None

	@staticmethod
	def from_args(args: argparse.Namespace) -> 'Ctx':
		def arg(name: str) -> str | None:
			return getattr(args, name, None)

		return Ctx(
			manager_repo=arg('manager_repo')
			or _env('MANAGER_REPO', 'GITHUB_REPOSITORY')
			or MANAGER_REPO_DEFAULT,
			executor_repo=arg('executor_repo')
			or _env('EXECUTOR_REPO')
			or EXECUTOR_REPO_DEFAULT,
			_pr_number=arg('pr_number') or _env('PR_NUMBER'),
			_head_ref=arg('head_ref') or _env('HEAD_REF', 'GITHUB_HEAD_REF'),
		)

	@property
	def pr_number(self) -> str:
		"""The PR number, or exit with a clear message if none was provided."""
		if not self._pr_number:
			raise SystemExit('no PR number: pass --pr or set $PR_NUMBER')
		return self._pr_number

	@property
	def pr_number_opt(self) -> str | None:
		"""The PR number if one was provided, else None (for no-PR no-op paths)."""
		return self._pr_number or None

	@property
	def head_ref(self) -> str:
		"""The PR head branch, falling back to the current git branch locally."""
		return self._head_ref or current_branch()


# Every `gh` call routed here is an idempotent read (`gh api` GET, `gh pr list`)
# or a create/force-update guarded by its caller, so a transient failure is safe
# to retry. The one we actually hit is GitHub handing back a truncated/empty body,
# which gh reports as `unexpected end of JSON input` and which killed the whole
# executor-precondition gate on a single hiccup; 5xx, rate-limit and network blips
# are retried for the same reason. A terminal 4xx (notably the 404 that
# `allow_missing` callers expect for an absent line) is not retried, so that fast
# path stays fast.
_RETRY_ATTEMPTS = 4
_RETRY_BACKOFF_S = 2.0

_TRANSIENT_MARKERS = (
	'unexpected end of json input',
	'http 500',
	'http 502',
	'http 503',
	'http 504',
	'rate limit',
	'timeout',
	'timed out',
	'connection reset',
	'connection refused',
	'eof',
)


def _is_transient(r: subprocess.CompletedProcess) -> bool:
	blob = f'{r.stdout}\n{r.stderr}'.lower()
	return any(marker in blob for marker in _TRANSIENT_MARKERS)


def gh(
	*args: str, token: str | None = None, check: bool = True
) -> subprocess.CompletedProcess:
	"""`gh` with `GH_TOKEN` bound to `token`, or ambient auth when `token` is None.

	Retries transient failures (see `_TRANSIENT_MARKERS`) a few times with linear
	backoff; a terminal failure is returned as-is (or raised, when `check`),
	exactly as a single `subprocess.run(check=...)` would.
	"""
	env = os.environ if token is None else {**os.environ, 'GH_TOKEN': token}
	for attempt in range(_RETRY_ATTEMPTS):
		result = subprocess.run(
			['gh', *args],
			env=env,
			check=False,
			text=True,
			capture_output=True,
		)
		if result.returncode == 0 or not _is_transient(result):
			break
		if attempt + 1 < _RETRY_ATTEMPTS:
			delay = _RETRY_BACKOFF_S * (attempt + 1)
			print(
				f'gh {" ".join(args)}: transient failure, retrying in {delay:.0f}s '
				f'(attempt {attempt + 1}/{_RETRY_ATTEMPTS})',
				file=sys.stderr,
			)
			time.sleep(delay)
	if check and result.returncode != 0:
		raise subprocess.CalledProcessError(
			result.returncode, result.args, output=result.stdout, stderr=result.stderr
		)
	return result
