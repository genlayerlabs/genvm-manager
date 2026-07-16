#!/usr/bin/env python3

import argparse
import hashlib
import os
import posixpath
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse


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
	ls_tree = git_output(['git', 'ls-tree', 'HEAD', path], root)
	if not ls_tree:
		raise RuntimeError(f'submodule path is not present in HEAD: {path}')
	fields = ls_tree.split()
	if len(fields) < 3 or fields[0] != '160000':
		raise RuntimeError(f'path is not a submodule gitlink in HEAD: {path}')
	return fields[2]


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


def cache_path_for_url(root: Path, url: str) -> Path:
	parsed = urlparse(url)
	url_path = parsed.path if parsed.scheme else url.rsplit(':', 1)[-1]
	name = Path(url_path).name or 'repo'
	name = re.sub(r'\.git$', '', name)
	name = re.sub(r'[^A-Za-z0-9._-]+', '-', name).strip('-') or 'repo'
	digest = hashlib.sha256(url.encode()).hexdigest()[:16]
	return root / '.git' / 'genvm-submodule-cache' / f'{name}-{digest}.git'


def ensure_cache(root: Path, url: str, hashes: list[str], env: dict[str, str]) -> Path:
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

	run(
		['git', '-C', str(cache), 'fetch', '--no-tags', 'origin', *hashes],
		root,
		env,
	)
	return cache


def set_remote(repo: Path, name: str, url: str, env: dict[str, str]) -> None:
	remote_exists = (
		subprocess.run(
			['git', '-C', str(repo), 'remote', 'get-url', name],
			stdout=subprocess.DEVNULL,
			stderr=subprocess.DEVNULL,
		).returncode
		== 0
	)
	if remote_exists:
		run(['git', '-C', str(repo), 'remote', 'set-url', name, url], repo, env)
	else:
		run(['git', '-C', str(repo), 'remote', 'add', name, url], repo, env)


def update_submodules_with_cache(root: Path, env: dict[str, str]) -> None:
	submodules = get_submodule_git_info(root)
	by_url: dict[str, list[SubmoduleGitInfo]] = {}
	for submodule in submodules:
		by_url.setdefault(submodule.url, []).append(submodule)

	cache_by_url = {
		url: ensure_cache(
			root,
			url,
			sorted({submodule.gitlink_hash for submodule in url_submodules}),
			env,
		)
		for url, url_submodules in by_url.items()
	}

	for submodule in submodules:
		cache = cache_by_url[submodule.url]
		run(
			[
				'git',
				'submodule',
				'update',
				'--init',
				'--recursive',
				'--reference',
				str(cache),
				'--depth',
				'1',
				'--no-fetch',
				'--',
				submodule.path,
			],
			root,
			env,
		)
		set_remote(root / submodule.path, 'origin', submodule.url, env)
		set_remote(root / submodule.path, 'cache', str(cache), env)


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
