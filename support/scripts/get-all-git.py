#!/usr/bin/env python3

import argparse
import contextlib
import fcntl
import hashlib
import os
import posixpath
import re
import shlex
import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

# Namespace of the cache-side refs that keep the pinned commits reachable.
PIN_REF_PREFIX = 'refs/genvm/pins/'
# Where the refs of a checkout converted to a worktree are mirrored. Namespaced
# by manager checkout as well as by path: several manager worktrees share one
# cache, and each converts the same submodule paths.
CONVERTED_REF_PREFIX = 'refs/genvm/converted/'


@dataclass(frozen=True)
class RepoGitInfo:
	path: str
	remote_url: str
	gitlink_hash: str


@dataclass(frozen=True)
class SubmoduleGitInfo:
	name: str
	path: str
	url: str
	gitlink_hash: str


def run(cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
	print(f'+ {shlex.join(cmd)}', flush=True)
	subprocess.run(cmd, check=True, cwd=cwd, env=env)


def git_output(cmd: list[str], cwd: Path) -> str:
	return subprocess.run(
		cmd, check=True, cwd=cwd, text=True, capture_output=True
	).stdout.strip()


def git_output_bytes(cmd: list[str], cwd: Path) -> bytes:
	return subprocess.run(cmd, check=True, cwd=cwd, stdout=subprocess.PIPE).stdout


def repo_root(value: str | None) -> Path:
	if value is not None:
		return Path(value).resolve()

	workspace = os.environ.get('GITHUB_WORKSPACE')
	if workspace:
		return Path(workspace).resolve()

	return Path(__file__).resolve().parents[2]


def resolve_submodule_url(url: str, parent_url: str) -> str:
	if not (url.startswith('./') or url.startswith('../')):
		return url

	parsed = urlparse(parent_url)
	if parsed.scheme:
		path = posixpath.normpath(posixpath.join(parsed.path, url))
		return urlunparse(parsed._replace(path=path))

	scp_like = re.fullmatch(r'([^/][^:]*):(.*)', parent_url)
	if scp_like:
		prefix, path = scp_like.groups()
		return f'{prefix}:{posixpath.normpath(posixpath.join(path, url))}'

	return str((Path(parent_url) / url).resolve())


def _read_gitmodules_entries(root: Path) -> dict[str, dict[str, str]]:
	out = git_output_bytes(
		[
			'git',
			'config',
			'--file',
			'.gitmodules',
			'--null',
			'--get-regexp',
			r'^submodule\..*\.(path|url)$',
		],
		root,
	)
	result: dict[str, dict[str, str]] = {}
	for item in out.rstrip(b'\0').split(b'\0'):
		if not item:
			continue
		key_raw, value_raw = item.split(b'\n', 1)
		key = key_raw.decode()
		value = value_raw.decode()
		prefix, field = key.rsplit('.', 1)
		name = prefix.removeprefix('submodule.')
		result.setdefault(name, {})[field] = value
	return result


def _gitlink_hash(root: Path, path: str) -> str:
	"""
	The commit ``path`` is pinned to, read from the index like git does.

	Staged and committed agree except right after a gitlink bump, where the
	index is the one the next commit will carry.
	"""
	entry = git_output(['git', 'ls-files', '--stage', '--', path], root)
	if not entry:
		raise RuntimeError(f'submodule path is not present in the index: {path}')
	# One line per conflict side when unmerged; any pick would be arbitrary.
	lines = entry.splitlines()
	if len(lines) > 1:
		raise RuntimeError(f'submodule is unmerged in the index: {path}')
	fields = lines[0].split()
	if len(fields) < 3 or fields[0] != '160000':
		raise RuntimeError(f'path is not a submodule gitlink: {path}')
	return fields[1]


def get_submodule_git_info(root: Path) -> list[SubmoduleGitInfo]:
	root = root.resolve()
	parent_url = git_output(['git', 'config', '--get', 'remote.origin.url'], root)
	result = []
	for name, data in _read_gitmodules_entries(root).items():
		path = data['path']
		url = resolve_submodule_url(data['url'], parent_url)
		result.append(
			SubmoduleGitInfo(
				name=name,
				path=path,
				url=url,
				gitlink_hash=_gitlink_hash(root, path),
			)
		)
	return result


def get_all_repo_git_info(root: Path) -> list[RepoGitInfo]:
	root = root.resolve()
	result = [
		RepoGitInfo(
			path='.',
			remote_url=git_output(['git', 'config', '--get', 'remote.origin.url'], root),
			gitlink_hash=git_output(['git', 'rev-parse', 'HEAD'], root),
		)
	]

	for submodule in get_submodule_git_info(root):
		result.append(
			RepoGitInfo(
				path=submodule.path,
				remote_url=submodule.url,
				gitlink_hash=submodule.gitlink_hash,
			)
		)

	return result


def git_common_dir(root: Path) -> Path:
	return Path(
		git_output(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'], root)
	)


def cache_path_for_url(root: Path, url: str) -> Path:
	parsed = urlparse(url)
	url_path = parsed.path if parsed.scheme else url.rsplit(':', 1)[-1]
	name = Path(url_path).name or 'repo'
	name = re.sub(r'\.git$', '', name)
	name = re.sub(r'[^A-Za-z0-9._-]+', '-', name).strip('-') or 'repo'
	digest = hashlib.sha256(url.encode()).hexdigest()[:16]
	return git_common_dir(root) / 'genvm-submodule-cache' / f'{name}-{digest}.git'


@contextlib.contextmanager
def cache_lock(root: Path) -> Iterator[None]:
	"""
	Serialize the shared submodule cache against another copy of this script.

	CI runs it from several jobs against the same checkout, and both the bare
	cache and the worktrees checked out of it are mutated in place: an
	interleaved init/fetch loses to git's own lockfiles, or leaves a cache
	another job is already reading from half-updated.
	"""
	lock = git_common_dir(root) / 'genvm-submodule-cache.lock'
	with open(lock, 'w') as handle:
		try:
			fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except BlockingIOError:
			print(f'+ waiting for {lock}', flush=True)
			fcntl.flock(handle, fcntl.LOCK_EX)
		yield


def has_commit(repo: Path, commit: str) -> bool:
	return (
		subprocess.run(
			['git', '-C', str(repo), 'cat-file', '-e', f'{commit}^{{commit}}'],
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		).returncode
		== 0
	)


def open_cache(root: Path, url: str, env: dict[str, str]) -> Path:
	"""
	Create (if needed) and configure the bare cache repo for ``url``.

	Every submodule sharing this URL is checked out of this one repository, so
	its objects are fetched once and its refs are what keeps them alive.
	"""
	cache = cache_path_for_url(root, url)
	cache.parent.mkdir(parents=True, exist_ok=True)
	if not (cache / 'HEAD').exists():
		run(['git', 'init', '--bare', str(cache)], root, env)

	remote_exists = (
		subprocess.run(
			['git', '-C', str(cache), 'remote', 'get-url', 'origin'],
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		).returncode
		== 0
	)
	if remote_exists:
		run(['git', '-C', str(cache), 'remote', 'set-url', 'origin', url], root, env)
	else:
		run(['git', '-C', str(cache), 'remote', 'add', 'origin', url], root, env)
	# A bare repo has no refspec by default, so a plain `git fetch` from an
	# executor checkout would update nothing. Give it the non-bare one.
	run(
		[
			'git',
			'-C',
			str(cache),
			'config',
			# Plain `config` fails once the key holds a second refspec.
			'--replace-all',
			'remote.origin.fetch',
			'+refs/heads/*:refs/remotes/origin/*',
		],
		root,
		env,
	)
	# Off by default in a bare repo, which would leave a commit made in a
	# worktree with no trace at all once the checkout is moved onto a new
	# gitlink. A non-bare submodule clone logged it, so keep that.
	run(['git', '-C', str(cache), 'config', 'core.logAllRefUpdates', 'true'], root, env)
	# Drop administrative entries of checkouts that are gone (a deleted manager
	# worktree), so re-adding the same path does not collide with a stale one.
	run(['git', '-C', str(cache), 'worktree', 'prune'], root, env)
	return cache


def fetch_pins(
	root: Path, cache: Path, submodules: list[SubmoduleGitInfo], env: dict[str, str]
) -> None:
	"""
	Make sure every pinned commit is in ``cache``, and keep it reachable.

	An unreferenced commit is only kept alive by the worktree HEADs pointing at
	it, and those checkouts do not exist yet — hence a ref per pin.
	"""
	missing = sorted(
		{s.gitlink_hash for s in submodules if not has_commit(cache, s.gitlink_hash)}
	)
	if missing:
		run(['git', '-C', str(cache), 'fetch', '--no-tags', 'origin', *missing], root, env)

	for submodule in submodules:
		run(
			[
				'git',
				'-C',
				str(cache),
				'update-ref',
				f'{PIN_REF_PREFIX}{submodule.path}',
				submodule.gitlink_hash,
			],
			root,
			env,
		)


def repo_gitdir(path: Path) -> Path | None:
	"""Absolute git dir of the checkout rooted at ``path``, else None."""
	if not (path / '.git').exists():
		return None
	result = subprocess.run(
		['git', '-C', str(path), 'rev-parse', '--path-format=absolute', '--git-dir'],
		capture_output=True,
		text=True,
	)
	if result.returncode != 0:
		return None
	return Path(result.stdout.strip()).resolve()


def current_branch(path: Path) -> str:
	"""The branch checked out at ``path``, or '' when HEAD is detached."""
	result = subprocess.run(
		['git', '-C', str(path), 'symbolic-ref', '--quiet', '--short', 'HEAD'],
		capture_output=True,
		text=True,
	)
	return result.stdout.strip() if result.returncode == 0 else ''


def ref_exists(repo: Path, ref: str) -> bool:
	return (
		subprocess.run(
			['git', '-C', str(repo), 'show-ref', '--verify', '--quiet', ref],
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		).returncode
		== 0
	)


def has_staged_changes(path: Path) -> bool:
	return (
		subprocess.run(
			['git', '-C', str(path), 'diff', '--cached', '--quiet'],
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		).returncode
		!= 0
	)


def clear_staging(staging: Path, name: str) -> None:
	"""
	Remove ``staging``, provided it is a leftover of an interrupted conversion.

	Which holds one `--no-checkout` worktree, so at most a `.git` file. Anything
	else under that name is somebody's data.
	"""
	expected = {staging / name, staging / name / '.git'}
	unexpected = sorted(p for p in staging.rglob('*') if p not in expected)
	if unexpected:
		raise RuntimeError(
			f'{staging} is not a leftover conversion staging directory '
			f'({unexpected[0]} is not part of one); move it aside and re-run'
		)
	shutil.rmtree(staging)


def adopt_as_worktree(
	root: Path, cache: Path, dest: Path, head: str, env: dict[str, str]
) -> None:
	"""
	Give the populated directory ``dest`` the `.git` of a worktree at ``head``.

	`git worktree add` cannot be pointed at a directory that has files in it, so
	the worktree is created empty beside it and only its `.git` file moves. No
	file under ``dest`` is read, written or removed.
	"""
	# Same basename as the checkout, so the administrative entry the cache ends
	# up with is named after the submodule rather than after this staging path.
	staging = dest.parent / f'.genvm-convert-{dest.name}'
	if staging.exists():
		clear_staging(staging, dest.name)
	# Removing the staging tree of an interrupted conversion strands its entry,
	# and `worktree add` refuses to reuse a path that is still registered.
	run(['git', '-C', str(cache), 'worktree', 'prune'], root, env)
	run(
		[
			'git',
			'-C',
			str(cache),
			'worktree',
			'add',
			'--detach',
			'--no-checkout',
			str(staging / dest.name),
			head,
		],
		root,
		env,
	)
	# Fill the index `--no-checkout` left empty before the swap, so the state
	# after it is complete. The index is built from HEAD alone; that the staging
	# tree has no files in it does not matter, and none of the real ones move.
	run(
		['git', '-C', str(staging / dest.name), 'reset', '--mixed', '--quiet', 'HEAD'],
		root,
		env,
	)
	# One atomic rename over the old gitlink file: an interrupted conversion
	# leaves a checkout that still belongs to one of the two repos, never one
	# with no `.git` at all.
	os.replace(staging / dest.name / '.git', dest / '.git')
	# Before the staging path goes, or the next conversion's `worktree prune`
	# drops an entry naming it and strands `dest` on a deleted gitdir.
	run(['git', '-C', str(cache), 'worktree', 'repair', str(dest)], root, env)
	shutil.rmtree(staging)


def convert_from_dangling(
	root: Path, cache: Path, submodule: SubmoduleGitInfo, env: dict[str, str]
) -> None:
	"""
	Re-attach a checkout whose `.git` names a gitdir that is gone.

	The files are all there is to go on, so the checkout is re-attached at the
	gitlink. Files that the old checkout had at some other commit stay as they
	are and show up as ordinary modifications.
	"""
	adopt_as_worktree(root, cache, root / submodule.path, submodule.gitlink_hash, env)


def convert_to_worktree(
	root: Path,
	cache: Path,
	submodule: SubmoduleGitInfo,
	gitdir: Path | None,
	env: dict[str, str],
) -> None:
	"""
	Re-point an existing checkout at ``cache``, keeping every file in place.

	Deleting and re-adding the directory would take the ignored content with it
	— the materialized third-party trees above all — so only the `.git` file is
	replaced: `git worktree add` cannot be pointed at a populated directory, but
	an empty worktree can be created next to it and its `.git` moved over. The
	abandoned gitdir may hold branches that exist nowhere else, so its refs are
	mirrored into the cache first. ``gitdir`` is None when that file names one
	that no longer exists, which leaves nothing to salvage but the files.
	"""
	dest = root / submodule.path
	if (dest / '.git').is_dir():
		raise RuntimeError(
			f'{submodule.path} is a standalone clone, not a submodule checkout; '
			f'move it aside and re-run'
		)

	print(f'+ converting {submodule.path} to a worktree of {cache}', flush=True)
	if gitdir is None:
		# Nothing is left to salvage but the files: whatever refs that gitdir
		# held are wherever it went.
		convert_from_dangling(root, cache, submodule, env)
		return

	# The old index is abandoned with the gitdir, and `reset` rebuilds it from
	# HEAD: unstaged and untracked work survives that, staged work would not.
	if has_staged_changes(dest):
		raise RuntimeError(
			f'{submodule.path} has staged changes and cannot be converted to a '
			f'worktree; commit or unstage them and re-run'
		)

	branch = current_branch(dest)
	head = git_output(['git', 'rev-parse', 'HEAD'], dest)
	digest = hashlib.sha256(str(root).encode()).hexdigest()[:16]
	namespace = f'{CONVERTED_REF_PREFIX}{digest}/{submodule.path}/'
	# Everything under `refs/`, not just the branches: a stash, a local tag or a
	# note can be the only thing holding on to a commit.
	run(
		[
			'git',
			'-C',
			str(cache),
			'fetch',
			'--no-tags',
			str(gitdir),
			f'+refs/*:{namespace}*',
			f'+HEAD:{namespace}HEAD',
		],
		root,
		env,
	)

	adopt_as_worktree(root, cache, dest, head, env)

	# The worktree starts detached; put the branch back so a checkout converted
	# mid-feature comes out on the branch it was on. The cache is shared, so a
	# name already taken there belongs to another checkout and stays untouched.
	if not branch:
		return
	if ref_exists(cache, f'refs/heads/{branch}'):
		print(
			f'+ {submodule.path} was on {branch}, which the cache already has; '
			f'leaving it detached, its refs are under {namespace}',
			flush=True,
		)
	else:
		run(['git', '-C', str(dest), 'checkout', '-b', branch], root, env)


def convert_if_needed(
	root: Path, cache: Path, submodule: SubmoduleGitInfo, env: dict[str, str]
) -> None:
	"""Convert the checkout at ``submodule.path`` unless it is already ours."""
	dest = root / submodule.path
	if not (dest / '.git').exists():
		return
	gitdir = repo_gitdir(dest)
	if gitdir is None or cache.resolve() not in gitdir.parents:
		convert_to_worktree(root, cache, submodule, gitdir, env)


def checkout_worktree(
	root: Path, cache: Path, submodule: SubmoduleGitInfo, env: dict[str, str]
) -> None:
	"""Make ``submodule.path`` a worktree of ``cache`` parked on the gitlink."""
	dest = root / submodule.path
	convert_if_needed(root, cache, submodule, env)
	gitdir = repo_gitdir(dest)

	if gitdir is None:
		if dest.exists() and any(dest.iterdir()):
			raise RuntimeError(f'{submodule.path} is not empty and is not a git checkout')
		run(
			[
				'git',
				'-C',
				str(cache),
				'worktree',
				'add',
				'--detach',
				str(dest),
				submodule.gitlink_hash,
			],
			root,
			env,
		)
		return

	# Idempotent, and the only thing that fixes the entry after the manager
	# checkout is moved on disk or a conversion is interrupted before this step.
	run(['git', '-C', str(cache), 'worktree', 'repair', str(dest)], root, env)

	if git_output(['git', 'rev-parse', 'HEAD'], dest) != submodule.gitlink_hash:
		run(
			['git', '-C', str(dest), 'checkout', '--detach', submodule.gitlink_hash],
			root,
			env,
		)


def update_submodules_with_cache(root: Path, env: dict[str, str]) -> None:
	with cache_lock(root):
		_update_submodules_with_cache(root, env)


def _update_submodules_with_cache(root: Path, env: dict[str, str]) -> None:
	submodules = get_submodule_git_info(root)
	by_url: dict[str, list[SubmoduleGitInfo]] = {}
	for submodule in submodules:
		by_url.setdefault(submodule.url, []).append(submodule)

	cache_by_url = {url: open_cache(root, url, env) for url in by_url}

	# Convert before fetching: a checkout being converted hands its refs to the
	# cache, and one of them may be the pinned commit itself — an unpushed
	# gitlink bump, which no fetch from `origin` could resolve.
	for submodule in submodules:
		convert_if_needed(root, cache_by_url[submodule.url], submodule, env)

	for url, url_submodules in by_url.items():
		fetch_pins(root, cache_by_url[url], url_submodules, env)

	for submodule in submodules:
		checkout_worktree(root, cache_by_url[submodule.url], submodule, env)


def main() -> None:
	parser = argparse.ArgumentParser(
		description='Checkout git submodules and vendored third-party trees.'
	)
	parser.add_argument(
		'--repo-root',
		help='repository root; defaults to GITHUB_WORKSPACE or this script location',
	)
	parser.add_argument(
		'--third-party',
		default='--all',
		help='arguments for `git third-party update`; use "none" to skip',
	)
	args = parser.parse_args()

	root = repo_root(args.repo_root)
	env = os.environ.copy()
	git_third_party = root / 'support' / 'tools' / 'git-third-party'
	env['PATH'] = f'{git_third_party}{os.pathsep}{env.get("PATH", "")}'

	update_submodules_with_cache(root, env)

	if args.third_party == 'none':
		return

	third_party_args = shlex.split(args.third_party)
	for cfg in sorted(root.glob('executors/*/.git-third-party/config.json')):
		executor = cfg.parent.parent
		run(['git', 'third-party', 'update', *third_party_args], executor, env)


if __name__ == '__main__':
	main()
