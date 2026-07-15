#!/usr/bin/env python3
"""Collect per-repo branch movement for a manager PR.

A manager PR (e.g. on the v0.6 line) bundles work across several repos: the
manager itself and every active executor line it ships (v0.2, v0.3, ...), each
an `executors/v<X>.x` submodule. This module reports, for each of those repos,
how the PR head moved relative to the base branch:

- base_sha: where the base side sits
- head_sha: where the PR head sits
- ahead_by: commits the head is ahead of base (new work)
- behind_by: commits the head is behind base (base advanced underneath it)
- pr_url: the open PR for this repo's head_ref -> base_ref, if any (the manager PR
itself for "."; the executor mirror PR for a line)

The manager is keyed as "." and compared branch-to-branch (base branch tip vs
PR head). Each executor line is keyed by its submodule path and compared
gitlink-to-gitlink: base_sha/head_sha are the submodule commits recorded at the
manager base tip and at the PR head, and ahead_by/behind_by are measured inside
the executor repo. A line whose gitlink is unchanged reports 0/0; use `.moved`
to test that.

Everything is read through the `gh` API, so no manager or submodule checkout is
needed — this is safe to call from a `pull_request_target` workflow that never
runs PR code. The manager repo is read with a manager-scoped token; the executor
repo needs a token with access to it (the default GITHUB_TOKEN is manager-scoped
and cannot reach another repo), exactly as tools/open_executor_prs.py does.

Two shas that differ but cannot be compared (the executor head commit was not
pushed to the executor repo, or the line is brand new / removed at one side)
leave ahead_by/behind_by as None — `.moved` is still meaningful.

Env (for `from_env`): PR_NUMBER, MANAGER_REPO (or GITHUB_REPOSITORY),
EXECUTOR_REPO, MANAGER_TOKEN, EXECUTOR_TOKEN.
"""

import dataclasses
import json
import os
import subprocess
import sys
import time

from tools.versions import active_versions as configured_active_versions

MANAGER_REPO_DEFAULT = 'genlayerlabs/genvm-manager'
EXECUTOR_REPO_DEFAULT = 'genlayerlabs/genvm-executor'

MANAGER_PATH = '.'


@dataclasses.dataclass(frozen=True)
class RepoInfo:
	"""How one repo's PR head moved relative to its base branch.

	`base_sha`/`head_sha` are None when the path is absent at that side (a line
	added or removed by the PR). `ahead_by`/`behind_by` are None when the two
	shas differ but are not comparable (the head commit is not on the executor
	remote, or one side is absent).
	"""

	path: str  # "." for the manager, "executors/v<X>.x" for an executor line
	repo: str  # GitHub slug the shas live in
	line: str | None  # None for the manager, "v<X>" for an executor line
	base_ref: str  # base branch the head is compared against
	head_ref: str  # head branch (manager PR head; `pr/<line>/<head>` for a line)
	base_sha: str | None
	head_sha: str | None
	ahead_by: int | None
	behind_by: int | None
	pr_url: str | None  # open PR head_ref -> base_ref, or None if none is open

	@property
	def moved(self) -> bool:
		"""The head points somewhere other than the base."""
		return self.head_sha != self.base_sha

	@property
	def has_pr(self) -> bool:
		"""An open PR for this repo's head_ref -> base_ref exists."""
		return self.pr_url is not None

	@property
	def rebased(self) -> bool:
		"""Head contains the base tip (not behind). A positive behind_by means it
		needs a rebase; unknown (None) is left to the synced check."""
		return not self.behind_by

	@property
	def synced(self) -> bool:
		"""The head gitlink is present in the executor repo (comparable to base), so
		it can land together with the manager commit. Being ahead of base is normal —
		manager and executors land almost-atomically — so only a head commit missing
		from the executor repo (ahead_by is None) is unsynced."""
		return self.ahead_by is not None

	def as_dict(self) -> dict:
		"""Plain-dict form (all fields plus the derived flags) for JSON output."""
		return {
			**dataclasses.asdict(self),
			'moved': self.moved,
			'has_pr': self.has_pr,
			'rebased': self.rebased,
			'synced': self.synced,
		}


# Every `gh` call here is an idempotent read (`gh api` GET, `gh pr list`), so a
# transient failure is safe to retry. The one we actually hit is GitHub handing
# back a truncated/empty body, which gh reports as `unexpected end of JSON input`
# and which killed the whole executor-precondition gate on a single hiccup; 5xx,
# rate-limit and network blips are retried for the same reason. A terminal 4xx
# (notably the 404 that `allow_missing` callers expect for an absent line) is not
# retried, so that fast path stays fast.
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


def gh(*args: str, token: str, check: bool = True) -> subprocess.CompletedProcess:
	"""`gh` with GH_TOKEN bound to the given token (manager- or executor-scoped).

	Retries transient failures (see `_TRANSIENT_MARKERS`) a few times with linear
	backoff; a terminal failure is returned as-is (or raised, when `check`),
	exactly as a single `subprocess.run(check=...)` would.
	"""
	for attempt in range(_RETRY_ATTEMPTS):
		result = subprocess.run(
			['gh', *args],
			env={**os.environ, 'GH_TOKEN': token},
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


def _api(*args: str, token: str, allow_missing: bool = False):
	"""`gh api` returning parsed JSON, or None on a 404 when allow_missing."""
	r = gh('api', *args, token=token, check=not allow_missing)
	if r.returncode != 0:
		# allow_missing: treat any failure (typically HTTP 404 — a ref/path that
		# does not exist on that remote) as "absent" rather than an error.
		return None
	return json.loads(r.stdout)


def compare(
	repo: str, base: str, head: str, token: str, *, allow_missing: bool = False
):
	"""GitHub `compare` of `base...head` in `repo`.

	Returns `(base_tip_sha, ahead_by, behind_by)`, or None when the comparison is
	not possible (allow_missing). `base_tip_sha` is the base ref's own commit; the
	ahead/behind counts are relative to the merge base, matching git's
	`rev-list --left-right base...head`. Only the counts are used, so the 250-commit
	cap on the returned `commits` list is irrelevant.
	"""
	data = _api(
		f'repos/{repo}/compare/{base}...{head}', token=token, allow_missing=allow_missing
	)
	if data is None:
		return None
	return data['base_commit']['sha'], data['ahead_by'], data['behind_by']


def submodule_sha(manager_repo: str, path: str, ref: str, token: str) -> str | None:
	"""The executor gitlink (submodule commit) recorded at `manager_repo`'s `ref`.

	Reads the submodule pointer through the contents API (`type: submodule`), so no
	checkout is needed. None when the path is absent at that ref (a line the PR adds
	or removes).
	"""
	data = _api(
		f'repos/{manager_repo}/contents/{path}?ref={ref}', token=token, allow_missing=True
	)
	if data is None:
		return None
	return data['sha']


def existing_pr(repo: str, head: str, base: str, token: str) -> str | None:
	"""URL of the open PR `head -> base` in `repo`, or None if none is open."""
	url = gh(
		'pr',
		'list',
		'--repo',
		repo,
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
		token=token,
	).stdout.strip()
	return url or None


def manager_info(pr_number: str, manager_repo: str, manager_token: str) -> RepoInfo:
	"""RepoInfo for the manager repo itself (base branch tip vs PR head).

	The manager PR always exists (it is the one being inspected), so its `pr_url`
	is taken straight from the PR object rather than queried.
	"""
	pr = _api(f'repos/{manager_repo}/pulls/{pr_number}', token=manager_token)
	base_ref = pr['base']['ref']
	head_ref = pr['head']['ref']
	head_sha = pr['head']['sha']
	base_sha, ahead_by, behind_by = compare(
		manager_repo, base_ref, head_sha, manager_token
	)
	return RepoInfo(
		path=MANAGER_PATH,
		repo=manager_repo,
		line=None,
		base_ref=base_ref,
		head_ref=head_ref,
		base_sha=base_sha,
		head_sha=head_sha,
		ahead_by=ahead_by,
		behind_by=behind_by,
		pr_url=pr['html_url'],
	)


def executor_info(
	line: str,
	manager_repo: str,
	manager_base_sha: str,
	manager_head_sha: str,
	manager_head_ref: str,
	executor_repo: str,
	manager_token: str,
	executor_token: str,
) -> RepoInfo:
	"""RepoInfo for one executor line: its gitlink at the manager base vs head.

	The executor mirror branch is deterministically named `pr/<line>/<head>` off
	the manager PR's head ref (genvm_tool.common.Repo.feature_branch), so its
	`pr_url` is the open PR of that branch into `<line>-dev`, if any.
	"""
	path = f'executors/{line}.x'
	base_ref = f'{line}-dev'
	head_ref = f'pr/{line}/{manager_head_ref}'
	base_sha = submodule_sha(manager_repo, path, manager_base_sha, manager_token)
	head_sha = submodule_sha(manager_repo, path, manager_head_sha, manager_token)

	if base_sha is not None and head_sha is not None:
		if base_sha == head_sha:
			ahead_by, behind_by = 0, 0  # gitlink unchanged; no need to ask the executor
		else:
			# The gitlink moved. Ask the executor repo how far. allow_missing: the
			# head gitlink may not be pushed to the executor yet (a PR that bumped the
			# submodule pointer without pushing the executor side).
			cmp = compare(
				executor_repo, base_sha, head_sha, executor_token, allow_missing=True
			)
			ahead_by, behind_by = (None, None) if cmp is None else (cmp[1], cmp[2])
	else:
		# Line added (no base) or removed (no head): moved, but nothing to compare.
		ahead_by, behind_by = None, None

	return RepoInfo(
		path=path,
		repo=executor_repo,
		line=line,
		base_ref=base_ref,
		head_ref=head_ref,
		base_sha=base_sha,
		head_sha=head_sha,
		ahead_by=ahead_by,
		behind_by=behind_by,
		pr_url=existing_pr(executor_repo, head_ref, base_ref, executor_token),
	)


def collect(
	pr_number: str,
	*,
	manager_repo: str = MANAGER_REPO_DEFAULT,
	executor_repo: str = EXECUTOR_REPO_DEFAULT,
	manager_token: str,
	executor_token: str,
) -> dict[str, RepoInfo]:
	"""Branch movement for every repo a manager PR touches, keyed by path.

	"." is the manager; "executors/v<X>.x" is each active executor line (from
	.genvm-monorepo-root via tools.versions).
	"""
	manager = manager_info(pr_number, manager_repo, manager_token)
	assert manager.base_sha is not None  # manager base branch always exists
	infos: dict[str, RepoInfo] = {manager.path: manager}
	for version in configured_active_versions():
		info = executor_info(
			f'v{version}',
			manager_repo,
			manager.base_sha,
			manager.head_sha,
			manager.head_ref,
			executor_repo,
			manager_token,
			executor_token,
		)
		infos[info.path] = info
	return infos


def from_env(pr_number: str | None = None) -> dict[str, RepoInfo]:
	"""`collect` with arguments read from the workflow environment.

	`pr_number` overrides $PR_NUMBER when given (e.g. from a CLI argument).
	"""
	return collect(
		pr_number or os.environ['PR_NUMBER'],
		manager_repo=os.environ.get('MANAGER_REPO')
		or os.environ.get('GITHUB_REPOSITORY', MANAGER_REPO_DEFAULT),
		executor_repo=os.environ.get('EXECUTOR_REPO', EXECUTOR_REPO_DEFAULT),
		manager_token=os.environ['MANAGER_TOKEN'],
		executor_token=os.environ['EXECUTOR_TOKEN'],
	)
