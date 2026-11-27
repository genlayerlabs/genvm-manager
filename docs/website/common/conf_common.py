"""
Sphinx settings shared by every part of the docs site: manager, executor and runner pages.

A `conf.py` calls :func:`apply` with its own `globals()` and its position below
the docs version directory:

- ``manager/``
- ``executor/<version>/``
- ``runner/<id>/<hash>/``

The version directory's `index.html` lists its parts (`catalog.py`); the
switcher picks a version from the site root `versions.json`.
"""

import json
import os
import subprocess
import time
import typing
from pathlib import Path

COMMON_DIR = Path(__file__).resolve().parent
MANAGER_ROOT = next(
	p for p in COMMON_DIR.parents if (p / '.genvm-monorepo-root').exists()
)
MONOREPO = json.loads((MANAGER_ROOT / '.genvm-monorepo-root').read_text())

Kind = typing.Literal['manager', 'executor', 'runner']


def source_date_epoch() -> int:
	"""
	Commit time of the manager ``HEAD``, or now outside a git checkout
	"""
	try:
		out = subprocess.run(
			['git', '-C', str(MANAGER_ROOT), 'log', '-1', '--format=%ct'],
			capture_output=True,
			text=True,
			check=True,
		).stdout
		return int(out.strip())
	except (OSError, subprocess.CalledProcessError, ValueError):
		return int(time.time())


def current_line() -> str:
	(line,) = (
		line
		for line, entry in MONOREPO['executor-lines'].items()
		if entry['status'] == 'current'
	)
	return line


def executor_dir(line: str) -> Path:
	return MANAGER_ROOT / 'executors' / f'{line}.x'


def executor_version(line: str) -> str:
	manifest = json.loads((executor_dir(line) / 'manifest.json').read_text())
	return manifest['executor-version']


# The site directory `deploy-docs` publishes this build under
DOCS_VERSION = os.environ.get('DOCS_VERSION', 'main')

SITE_URL = 'https://' + os.environ.get('DOCS_DOMAIN', 'sdk.genlayer.com')

# The paths below are relative to the version directory
MANAGER_PATH = 'manager/'


def executor_path(version: str) -> str:
	return f'executor/{version}/'


def runner_path(runner_id: str, hash32: str) -> str:
	return f'runner/{runner_id}/{hash32}/'


def inventory(path: str) -> str | None:
	"""
	Local `objects.inv` of the site at `path` in the build, None until built
	"""
	out = os.environ.get('GENVM_DOCS_SITE_OUT')
	if out is None:
		return None
	inv = Path(out) / path / 'objects.inv'
	return str(inv) if inv.exists() else None


# How deep each kind's root sits below the version directory
_DEPTH: dict[Kind, int] = {'manager': 1, 'executor': 2, 'runner': 3}


def relative(from_kind: Kind, path: str) -> str:
	"""
	Relative URL from a `from_kind` site root to `path`, given from the version directory
	"""
	return '../' * _DEPTH[from_kind] + path


def apply(
	ns: dict[str, typing.Any],
	*,
	kind: Kind,
	title: str,
	crumbs: list[tuple[str, str]],
) -> None:
	"""
	:param ns: the calling ``conf.py``'s ``globals()``
	:param kind: which part of the site the build produces
	:param title: ``project`` shown in the navbar and page titles
	:param crumbs: ``(label, path from the version directory)`` pairs shown in the navbar
	"""
	# The nix shell pins SOURCE_DATE_EPOCH to 1980, and Sphinx rewrites the
	# copyright year to match it after this file runs
	epoch = source_date_epoch()
	os.environ['SOURCE_DATE_EPOCH'] = str(epoch)
	year = time.gmtime(epoch).tm_year
	ns['project'] = title
	ns['author'] = 'GenLayer Labs'
	ns['copyright'] = f'{year}, GenLayer Labs'
	ns['language'] = 'en'

	ns['html_theme'] = 'pydata_sphinx_theme'
	ns['templates_path'] = [str(COMMON_DIR / '_templates'), *ns.get('templates_path', [])]
	ns['html_static_path'] = [
		str(COMMON_DIR / '_static'),
		*ns.get('html_static_path', []),
	]
	ns['html_css_files'] = ['custom.css']
	ns['html_js_files'] = ['favicon-swap.js']
	ns['html_favicon'] = str(COMMON_DIR / '_static' / 'favicon.png')
	ns['html_show_sourcelink'] = False
	# Marks the latest hash of a runner in tables, colored by its executor line status
	ns['rst_prolog'] = ns.get('rst_prolog', '') + ''.join(
		f'\n.. role:: gl-latest-{status}\n   :class: gl-latest {status}\n'
		for status in sorted({e['status'] for e in MONOREPO['executor-lines'].values()})
	)

	ns['html_context'] = {
		**ns.get('html_context', {}),
		'gl_version_root': relative(kind, ''),
		'gl_crumbs': [(label, relative(kind, path)) for label, path in crumbs],
	}
	ns['html_theme_options'] = {
		'logo': {
			'image_light': '_static/logo-light.svg',
			'image_dark': '_static/logo-dark.svg',
			'text': title,
		},
		'show_nav_level': 2,
		'show_toc_level': 2,
		'navbar_start': ['navbar-logo', 'version-switcher'],
		'article_header_end': ['gl-site-nav'],
		'switcher': {
			'json_url': f'{SITE_URL}/versions.json',
			'version_match': DOCS_VERSION,
		},
		# The build must not depend on the published site
		'check_switcher': False,
		'navbar_end': ['theme-switcher', 'navbar-icon-links'],
		'icon_links': [
			{
				'name': 'GitHub',
				'url': 'https://github.com/genlayerlabs/genvm',
				'icon': 'fa-brands fa-github',
				'type': 'fontawesome',
			},
			{
				'name': 'Discord',
				'url': 'https://discord.gg/8Jm4v89VAu',
				'icon': 'fa-brands fa-discord',
				'type': 'fontawesome',
			},
			{
				'name': 'Telegram',
				'url': 'https://t.me/genlayer',
				'icon': 'fa-brands fa-telegram',
				'type': 'fontawesome',
			},
			{
				'name': 'X (Twitter)',
				'url': 'https://x.com/GenLayer',
				'icon': 'fa-brands fa-x-twitter',
				'type': 'fontawesome',
			},
		],
		'footer_start': ['copyright'],
		'footer_end': [],
		**ns.get('html_theme_options', {}),
	}
