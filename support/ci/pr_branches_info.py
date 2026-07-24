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

Inputs (repo slugs, PR number, tokens) are resolved through `gh_common`
(arg > env > default); tokens are optional and fall back to ambient `gh` auth.
Use `from_ctx(gh_common.Ctx.from_args(args))` from a tool.
"""

import dataclasses
import json

import gh_common
from gh_common import (  # re-exported: callers use pr_branches_info.gh / *_DEFAULT
	EXECUTOR_REPO_DEFAULT,
	MANAGER_REPO_DEFAULT,
	gh,
)
from tools.versions import active_versions as configured_active_versions

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


def _api(*args: str, token: str | None = None, allow_missing: bool = False):
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
	manager_token: str | None = None,
	executor_token: str | None = None,
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


def from_ctx(ctx: gh_common.Ctx) -> dict[str, RepoInfo]:
	"""`collect` with repos/PR/tokens resolved from a shared `gh_common.Ctx`."""
	return collect(
		ctx.pr_number,
		manager_repo=ctx.manager_repo,
		executor_repo=ctx.executor_repo,
		manager_token=gh_common.manager_token(),
		executor_token=gh_common.executor_token(),
	)
