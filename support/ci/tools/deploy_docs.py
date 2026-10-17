#!/usr/bin/env python3
"""
Place a built docs tree into a checkout of the sdk.genlayer.com site repo.

That repo publishes `_site/` to GitHub Pages, one directory per docs version
(`_site/main`, `_site/v0.3.x`, ...) plus a root `_site/versions.json` that feeds
the version switcher every built page links to. This tool only rewrites those
files by default; `--push` publishes from a disposable, clean site checkout.

The version directory is replaced wholesale rather than merged, so pages deleted
upstream do not linger.

`versions.json` keeps its existing order — it is hand-curated (newest train
first, point releases under it) and re-sorting it on every nightly would produce
noise. A version that is not listed yet is inserted directly after `main`.
"""

import argparse
import json
import pathlib
import re
import shutil
import subprocess
import time

import ci_lib

# Directory the site repo publishes; everything below is relative to it.
SITE_SUBDIR = '_site'

VERSIONS_JSON = 'versions.json'

SITE_URL = 'https://sdk.genlayer.com'


def entry_for(version: str, preferred: bool) -> dict:
	name = 'latest (main)' if version == 'main' else version
	return {
		'name': name,
		'version': version,
		'url': f'{SITE_URL}/{version}/',
		'preferred': preferred,
	}


def update_versions(path, version: str, preferred: bool) -> None:
	"""
	Insert or refresh `version`'s entry, leaving every other entry untouched.

	`preferred` is what the switcher highlights, so at most one entry may carry it:
	passing it here clears it everywhere else. NOT passing it leaves the current
	highlight alone rather than clearing it — the nightly never passes the flag, and
	it must not strip `main`'s highlight every night.
	"""
	versions = json.loads(path.read_text()) if path.exists() else []

	new = entry_for(version, preferred)
	for index, existing in enumerate(versions):
		if existing.get('version') == version:
			if not preferred:
				new['preferred'] = bool(existing.get('preferred'))
			versions[index] = new
			break
	else:
		main_at = next(
			(i for i, e in enumerate(versions) if e.get('version') == 'main'),
			-1,
		)
		versions.insert(main_at + 1, new)
		print(f'{VERSIONS_JSON}: added `{version}`')

	if preferred:
		for existing in versions:
			if existing.get('version') != version:
				existing['preferred'] = False

	path.write_text(json.dumps(versions, indent=2) + '\n')


class DeployDocs(ci_lib.Tool):
	"""
	Copy a built docs tree into an sdk.genlayer.com checkout.
	"""

	def name(self) -> str:
		return 'deploy-docs'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		parser.add_argument(
			'--site',
			required=True,
			help=f'checkout of the site repo (the one holding {SITE_SUBDIR}/)',
		)
		parser.add_argument(
			'--version',
			default='main',
			help='directory to publish under, e.g. `main` or `v0.6.x` (default: main)',
		)
		parser.add_argument(
			'--html',
			default=str(ci_lib.ROOT_DIR / 'build' / 'doc' / 'html'),
			help='built docs tree to publish (default: build/doc/html)',
		)
		parser.add_argument(
			'--push',
			action='store_true',
			help='commit and push to origin/main, retrying races (disposable clean checkout only)',
		)
		parser.add_argument(
			'--preferred',
			action='store_true',
			help='make this the version the switcher highlights',
		)

	def handler(self, args: argparse.Namespace) -> int:
		# The version becomes a path component that is then rmtree'd, and it arrives
		# from a free-text workflow_dispatch input: `..` would delete the checkout.
		if not re.fullmatch(r'[A-Za-z0-9._-]+', args.version) or args.version in (
			'.',
			'..',
		):
			print(f'`{args.version}` is not a usable version directory name')
			return 1

		html = pathlib.Path(args.html).resolve()
		if not (html / 'index.html').is_file():
			print(
				f'{html} is not a built docs tree (no index.html); run `pipeline docs` first'
			)
			return 1

		site = pathlib.Path(args.site).resolve() / SITE_SUBDIR
		if not site.is_dir():
			print(f'{site} does not exist; is --site an sdk.genlayer.com checkout?')
			return 1

		if args.push:
			return self.publish(args, html, site)
		self.stage(args, html, site)
		return 0

	def stage(self, args, html, site):
		target = site / args.version
		if target.exists():
			shutil.rmtree(target)
		# `.doctrees` is sphinx's incremental-build cache, not part of the site.
		shutil.copytree(html, target, ignore=shutil.ignore_patterns('.doctrees'))
		print(f'published {html} -> {target}')

		update_versions(site / VERSIONS_JSON, args.version, args.preferred)

	def publish(self, args, html, site):
		def git(*command, **kwargs):
			return subprocess.run(['git', *command], cwd=site.parent, check=True, **kwargs)

		if git('status', '--porcelain', capture_output=True, text=True).stdout:
			print('--push requires a clean, disposable site checkout')
			return 1
		git('config', 'user.email', 'ci@genlayerlabs.com')
		git('config', 'user.name', 'CI worker')
		for attempt in range(5):
			git('fetch', 'origin', 'refs/heads/main')
			git('reset', '--hard', 'FETCH_HEAD')
			# Reapply to the latest tree so concurrent versions.json edits survive.
			self.stage(args, html, site)
			git('add', '-A', '_site')
			if not git(
				'diff', '--cached', '--name-only', capture_output=True, text=True
			).stdout:
				print('docs are unchanged; nothing to deploy')
				return 0
			git('commit', '-m', f'Deploy documentation version {args.version}')
			try:
				git('push', 'origin', 'HEAD:refs/heads/main')
				return 0
			except subprocess.CalledProcessError:
				if attempt == 4:
					print('docs push failed after 5 attempts')
					return 1
				print(f'docs push failed; retrying publication (attempt {attempt + 2}/5)')
				time.sleep(2**attempt)


COMMANDS = [DeployDocs()]
