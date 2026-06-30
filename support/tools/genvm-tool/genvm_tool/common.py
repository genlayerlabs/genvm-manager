"""Shared primitives: repo discovery and git queries.

A "repo" is the manager root plus every executor submodule
(`executors/<v>.x/`, discovered from `.genvm-monorepo-root`). The hook-execution
engine (tool profiles, filtering, runner, builtins) lives in
`genvm_tool.hook.engine`.
"""

import functools
import json
import os
import re
import subprocess
import types
from dataclasses import dataclass
from pathlib import Path

from . import formatter

MONOREPO_MARKER = '.genvm-monorepo-root'
# Per-repo project file (manager + every executor). Exec'd once before any
# subcommand runs; subcommands ask it for what they need — `hooks(ctx)` (commit
# hooks for that repo) and, on the manager, `tests(ctx)` (the test suite).
PROJECT_FILE = '.genvm-tool.py'
PRECOMMIT_SUBDIR = 'support/nix/precommit'
# Kept verbatim per the agreed cache location; sits in the gitignored `.direnv`.
CACHE_SUBDIR = '.direnv/genvm-git-helper'
DEFAULT_STAGE = 'pre-commit'


@functools.lru_cache(maxsize=None)
def command_to_executable(command: str) -> Path:
	"""Resolve a command to an absolute executable path, or raise."""
	from shutil import which

	path = which(command)
	if not path:
		raise ToolError(f'command not found in PATH: {command!r}')
	return Path(path).resolve()


@dataclass
class Context:
	"""Process-wide handles passed to every subcommand."""

	root: Path
	logger: formatter.Formatter
	printer: formatter.Sink
	# The manager's loaded `.genvm-tool.py` module (or None if absent). Populated
	# once in `__main__` before dispatch; subcommands query its exported funcs.
	project: types.ModuleType | None = None


class ToolError(Exception):
	"""User-facing error; `main` renders it and exits non-zero."""


@functools.lru_cache(maxsize=None)
def _load_project_cached(path_str: str) -> types.ModuleType | None:
	path = Path(path_str)
	if not path.is_file():
		return None
	module = types.ModuleType('genvm_tool_project')
	module.__dict__['__file__'] = str(path)
	# Top-level only defines functions, so this exec is cheap and dependency-free
	# (the heavy imports live inside `tests()`, run lazily by the test command).
	exec(compile(path.read_text(), str(path), 'exec'), module.__dict__)
	return module


def load_project(repo_root: Path) -> types.ModuleType | None:
	"""Load (and cache) a repo's `.genvm-tool.py`, or None if it has none."""
	return _load_project_cached(str((repo_root / PROJECT_FILE).resolve()))


def _bare(v: str) -> str:
	"""Strip an optional leading 'v', e.g. 'v0.3' -> '0.3'."""
	return v.lstrip('v')


def _ver_key(v: str):
	return tuple(int(x) for x in _bare(v).split('.'))


def find_root(start: Path | None = None) -> Path:
	"""Walk up from `start` (or $MONOREPO_ROOT / cwd) to the manager root."""
	override = os.environ.get('MONOREPO_ROOT')
	base = Path(start or override or Path.cwd()).resolve()
	for p in [base, *base.parents]:
		if (p / MONOREPO_MARKER).is_file():
			return p
	raise ToolError(f'not inside a genvm monorepo (no {MONOREPO_MARKER} from {base})')


@functools.lru_cache(maxsize=None)
def monorepo_config(root: Path) -> dict:
	"""Parsed `.genvm-monorepo-root` (executor trains + test-runner config)."""
	return json.loads((root / MONOREPO_MARKER).read_bytes())


def active_versions(root: Path) -> list[str]:
	"""Active executor trains from .genvm-monorepo-root, ascending, bare."""
	vers = (_bare(v) for v in monorepo_config(root).get('active-versions', []))
	return sorted(vers, key=_ver_key)


# Release/branch-model branches (`main`, a version branch `v0.3.x`, or a dev
# branch `v0.3-dev`) — already line-scoped, so they are never feature-namespaced.
_MODEL_BRANCH = re.compile(r'main|v[\d.]+\.x|v[\d.]+-dev')


class Repo:
	"""One git repo in the umbrella: the manager or an executor submodule."""

	def __init__(self, name: str, path: Path, rel: str):
		self.name = name  # 'manager' or e.g. 'executors/v0.3.x'
		self.path = path  # absolute working-tree root
		self.rel = rel  # '' for manager, else the manager-relative prefix

	@property
	def precommit_dir(self) -> Path:
		return self.path / PRECOMMIT_SUBDIR

	@property
	def cache_dir(self) -> Path:
		return self.path / CACHE_SUBDIR

	def is_checked_out(self) -> bool:
		# A submodule's `.git` is a gitdir file; the manager's is a directory.
		return (self.path / '.git').exists()

	def prefix(self, rel_path: str) -> str:
		"""Make a repo-relative path manager-root-relative."""
		return f'{self.rel}/{rel_path}' if self.rel else rel_path

	@property
	def line(self) -> str | None:
		"""The version-line tag (e.g. `v0.3`) for an executor submodule, or None
		for the manager."""
		m = re.fullmatch(r'executors/(v\d+\.\d+)\.x', self.name)
		return m.group(1) if m else None

	def feature_branch(self, name: str) -> str:
		"""Physical branch name for the logical feature branch `name` in this repo.

		The manager is its own repo, so it carries `name` verbatim. Every executor
		submodule, however, shares ONE remote (`genvm-executor`): a bare `name`
		would collide between two active lines that branch off it at once. So an
		executor feature branch is namespaced under `pr/<line>/` (e.g.
		`pr/v0.3/<name>`) — the `<line>` segment is what keeps the two lines
		distinct, matching how the release branches (`v0.3-dev`, `v0.3.x`) are
		already line-scoped and thus never clash. Release-model branches and an
		already-prefixed `name` pass through unchanged (idempotent).
		"""
		line = self.line
		if line is None or name.startswith(f'pr/{line}/') or _MODEL_BRANCH.fullmatch(name):
			return name
		return f'pr/{line}/{name}'


def discover_repos(root: Path, which: str = 'all') -> list[Repo]:
	repos = [Repo('manager', root, '')]
	for v in active_versions(root):
		rel = f'executors/v{v}.x'
		repos.append(Repo(rel, root / rel, rel))
	if which == 'all':
		return repos
	if which == 'manager':
		return repos[:1]
	for r in repos:
		if which in (r.name, r.rel):
			return [r]
	raise ToolError(f'unknown repo: {which!r} (have: {", ".join(r.name for r in repos)})')


def executor_rels(root: Path) -> set[str]:
	return {f'executors/v{v}.x' for v in active_versions(root)}


# --- git -------------------------------------------------------------------


def _git_z(repo: Repo, *args: str) -> list[str]:
	out = subprocess.run(
		['git', '-C', str(repo.path), *args, '-z'],
		capture_output=True,
		text=True,
		check=True,
	).stdout
	return [f for f in out.split('\0') if f]


def ls_files(repo: Repo, drop_rels: set[str] | None = None) -> list[str]:
	"""Tracked files in `repo` (repo-relative), dropping submodule gitlinks."""
	files = _git_z(repo, 'ls-files')
	if drop_rels:
		files = [f for f in files if f not in drop_rels]
	return files


def staged_files(repo: Repo) -> list[str]:
	"""Staged added/copied/modified files (repo-relative), regular files only."""
	files = _git_z(repo, 'diff', '--cached', '--name-only', '--diff-filter=ACM')
	# Drop the executor gitlink (a staged submodule bump is not a file).
	return [f for f in files if (repo.path / f).is_file()]
