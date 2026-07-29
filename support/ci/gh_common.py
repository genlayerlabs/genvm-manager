#!/usr/bin/env python3
"""
Shared GitHub context for CI tools: repos, PR number, head ref, tokens.

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

`gh()` traces every invocation to stderr (`+ gh ...`, and the exit code when
non-zero), so a CI log shows which API calls a tool actually made — including
the ones it expects to fail and handles with `check=False`.

A tool that resolves its context once per process calls `set_ctx()` in its
handler and then reads `repo()` / `pr_number()` / `gh_manager()` as free
functions. That pattern used to be copy-pasted (a private `_CTX`, `_ctx()`,
`repo()`, `pr_number()` and a bespoke `gh()` wrapper) into every tool module;
it lives here once instead.
"""

import argparse
import dataclasses
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.parse

MANAGER_REPO_DEFAULT = 'genlayerlabs/genvm-manager'
EXECUTOR_REPO_DEFAULT = 'genlayerlabs/genvm-executor'


def _env(*names: str) -> str | None:
	"""
	First set, non-empty value among env `names`, else None.
	"""
	for name in names:
		val = os.environ.get(name)
		if val:
			return val
	return None


def manager_token() -> str | None:
	"""
	Manager-repo token, or None for ambient `gh` auth. `MANAGER_TOKEN` is the
	CI name; `GH_TOKEN` is accepted so a single ambient token also works.
	"""
	return _env('MANAGER_TOKEN', 'GH_TOKEN')


def executor_token() -> str | None:
	"""
	Executor-repo token, or None for ambient `gh` auth (`EXECUTOR_TOKEN`).
	"""
	return _env('EXECUTOR_TOKEN')


def current_branch() -> str:
	"""
	Current git branch — the local fallback for a PR head ref.
	"""
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
	"""
	Register the common flags on `parser`. `--manager-repo` is always added;
	the rest are opt-out for the few tools that don't need them. Each defaults to
	None so `Ctx.from_args` can apply arg > env > default uniformly.
	"""
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
	"""
	Resolved GitHub inputs for a tool run (arg > env > default applied).
	"""

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
		"""
		The PR number, or exit with a clear message if none was provided.
		"""
		if not self._pr_number:
			raise SystemExit('no PR number: pass --pr or set $PR_NUMBER')
		return self._pr_number

	@property
	def pr_number_opt(self) -> str | None:
		"""
		The PR number if one was provided, else None (for no-PR no-op paths).
		"""
		return self._pr_number or None

	@property
	def head_ref(self) -> str:
		"""
		The PR head branch, falling back to the current git branch locally.
		"""
		return self._head_ref or current_branch()


# Retrying exists for one observed failure: GitHub handing back a truncated/empty
# body, which gh reports as `unexpected end of JSON input` and which killed the
# whole executor-precondition gate on a single hiccup. 5xx, rate-limit and network
# blips are retried for the same reason. A terminal 4xx (notably the 404 that
# `allow_missing` callers expect for an absent line) is not retried, so that fast
# path stays fast.
#
# Retrying is safe for an idempotent read (`gh api` GET, `gh pr list`) and for a
# force-update guarded by its caller. It is NOT safe for a non-idempotent write,
# because these markers are matched as substrings of the combined output and can
# misread a request that in fact succeeded — so a caller that creates a PR or
# posts a comment should pass `retry=False`. Not every such caller does yet
# (`tools/pr_branches.py` still retries `pr create`); tightening the markers and
# splitting reads from writes is tracked in GVM-328.
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
	*args: str, token: str | None = None, check: bool = True, retry: bool = True
) -> subprocess.CompletedProcess:
	"""
	`gh` with `GH_TOKEN` bound to `token`, or ambient auth when `token` is None.

	Retries transient failures (see `_TRANSIENT_MARKERS`) a few times with linear
	backoff; a terminal failure is returned as-is (or raised, when `check`),
	exactly as a single `subprocess.run(check=...)` would.

	`retry=False` disables that entirely, for a call whose replay is not safe —
	a non-idempotent write (posting a comment, creating a PR) would otherwise be
	performed twice when a *successful* request is misread as transient.
	"""
	env = os.environ if token is None else {**os.environ, 'GH_TOKEN': token}
	attempts = _RETRY_ATTEMPTS if retry else 1
	# Trace every call, like ci_lib.run's `+ ...`. On stderr, so a caller that
	# parses this process's stdout is unaffected. The token travels in the
	# environment, never in argv, so there is nothing to redact here.
	print(f'+ gh {" ".join(shlex.quote(a) for a in args)}', file=sys.stderr, flush=True)
	for attempt in range(attempts):
		result = subprocess.run(
			['gh', *args],
			env=env,
			check=False,
			text=True,
			capture_output=True,
		)
		if result.returncode == 0 or not _is_transient(result):
			break
		if attempt + 1 < attempts:
			delay = _RETRY_BACKOFF_S * (attempt + 1)
			print(
				f'  transient failure, retrying in {delay:.0f}s '
				f'(attempt {attempt + 1}/{attempts})',
				file=sys.stderr,
				flush=True,
			)
			time.sleep(delay)
	if result.returncode != 0:
		# Callers pass check=False for expected failures (a 404 for an absent
		# branch), so this is a note, not an error — but without it a `gh` that
		# fails silently by design leaves no trace of why the caller took the
		# other path.
		print(
			f'  gh exited {result.returncode}: {result.stderr.strip()}',
			file=sys.stderr,
			flush=True,
		)
	if check and result.returncode != 0:
		raise subprocess.CalledProcessError(
			result.returncode, result.args, output=result.stdout, stderr=result.stderr
		)
	return result


def api_path(path: str, **params: str | int) -> str:
	"""
	An `api` path with `params` appended as a properly encoded query string.

	Never interpolate a value into a path by hand: a check-run name such as
	`not rebased` contains a space, and `gh api` passes the query through
	verbatim — the resulting request does not fail, it **hangs** until the job's
	timeout. Anything that reaches a query string goes through here.

	`quote_via=quote`, so a space becomes `%20` rather than urlencode's default
	`+`. Both work against GitHub today, but `+`-as-space is an HTML-form
	convention a server is free not to apply, while `%20` is unambiguous.
	"""
	query = urllib.parse.urlencode(
		{k: str(v) for k, v in params.items()}, quote_via=urllib.parse.quote
	)
	if not query:
		return path
	sep = '&' if '?' in path else '?'
	return f'{path}{sep}{query}'


# --- per-run context ----------------------------------------------------------
# A tool process handles exactly one PR, so its resolved Ctx is set once in the
# handler and read as free functions from anywhere in the module. Kept here so
# the tools share one implementation instead of a private copy each.

_CTX: 'Ctx | None' = None


def set_ctx(ctx: 'Ctx') -> 'Ctx':
	"""
	Install the resolved context for this process. Call once, from the handler.
	"""
	global _CTX
	_CTX = ctx
	return ctx


def ctx() -> 'Ctx':
	"""
	The context `set_ctx` installed.
	"""
	assert _CTX is not None, 'gh_common.Ctx not initialized (handler must set_ctx)'
	return _CTX


def repo() -> str:
	"""
	Manager repo slug from the installed context.
	"""
	return ctx().manager_repo


def pr_number() -> str:
	"""
	PR number from the installed context; exits if none was provided.
	"""
	return ctx().pr_number


def gh_manager(*args: str, check: bool = True, retry: bool = True) -> str:
	"""
	`gh` with the manager token, returning stdout.

	The shape almost every tool wants. Pass `retry=False` for a non-idempotent
	write — see `gh`.
	"""
	return gh(*args, token=manager_token(), check=check, retry=retry).stdout


# Repository roles that count as "trusted to drive CI". `author_association` on
# an event payload is NOT a substitute: its MEMBER value only means "in the org",
# which says nothing about push access to this repo.
WRITE_ROLES = frozenset({'admin', 'maintain', 'write'})


def has_write_access(login: str) -> bool:
	"""
	Whether `login` has push rights on the manager repo.

	Used to authorize anything a user can trigger by interacting with a PR. A
	non-collaborator yields 403/404 from this endpoint, which is a `False`, not
	an error.
	"""
	r = gh('api', f'repos/{repo()}/collaborators/{login}/permission', check=False)
	if r.returncode != 0:
		return False
	return json.loads(r.stdout).get('permission') in WRITE_ROLES
