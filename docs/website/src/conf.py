import datetime
import json
import os
import re as _re
import urllib.request
from pathlib import Path

# The Python SDK reference (autodoc of the genlayer package) lives in the
# executor line that ships it — see executors/<line>.x/docs/website. This
# manager site documents the spec / impl-spec / overview only, so it carries no
# autodoc machinery. Cross-linking is one-way: the executor sub-sites reference
# this manager site via intersphinx; the manager does NOT reference them back.

project = 'GenVM SDK'

copyright_year = str(datetime.date.today().year)
if 'COPYRIGHT_YEAR' in os.environ:
	copyright_year = os.environ['COPYRIGHT_YEAR']


copyright = f'{copyright_year}, GenLayer Labs'
author = 'GenLayer Labs'
release = os.environ.get('DOCS_VERSION', 'main')
version = release

extensions = [
	'sphinxcontrib.mermaid',
	'sphinxcontrib.openapi',
	'myst_parser',
]

templates_path = ['_templates']
exclude_patterns = [
	'_build',
	'Thumbs.db',
	'.DS_Store',
	'*_generated.rst',
	'api',
	'impl-spec/appendix/runners-versions.rst',
]

language = 'en'

mermaid_version = '11.6.0'
mermaid_output_format = 'svg'
mermaid_params = ['--theme', 'dark', '--backgroundColor', 'transparent']

# html_theme = 'alabaster'
html_theme = 'pydata_sphinx_theme'
html_static_path = ['_static']

_docs_domain = os.environ.get('DOCS_DOMAIN', 'sdk.genlayer.com')

# Fetch remote versions and merge with local build, sorted by semver
try:
	_req = urllib.request.Request(
		f'https://{_docs_domain}/versions.json', headers={'User-Agent': 'sphinx-build'}
	)
	with urllib.request.urlopen(_req, timeout=5) as _resp:
		_remote_versions = json.loads(_resp.read())
except Exception:
	_remote_versions = []

# Add local build entry
_local_entry = {
	'name': f'{version} (local)',
	'version': version,
	'url': '/',
	'preferred': True,
}
_versions = [v for v in _remote_versions if v.get('version') != version] + [
	_local_entry
]


# Sort by semver descending, non-semver (like "main") at end
def _semver_sort_key(entry):
	m = _re.match(r'^v?(\d+)\.(\d+)\.(\d+)', entry.get('version', ''))
	if m:
		return (0, -int(m.group(1)), -int(m.group(2)), -int(m.group(3)))
	return (1, entry.get('version', ''))


_versions.sort(key=_semver_sort_key)

Path(__file__).parent.joinpath('_static', 'versions.json').write_text(
	json.dumps(_versions, indent=2)
)

html_theme_options = {
	'logo': {
		'image_light': f'/{release}/_static/logo-light.svg',
		'image_dark': f'/{release}/_static/logo-dark.svg',
	},
	'show_nav_level': 2,
	'show_toc_level': 2,
	'navbar_start': ['navbar-logo', 'version-switcher'],
	'navbar_end': ['theme-switcher', 'navbar-icon-links'],
	'icon_links': [
		{
			'name': 'Full docs for LLMs',
			'url': f'/{release}/_static/llms.txt',
			'icon': 'fa-solid fa-robot',
			'type': 'fontawesome',
		},
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
	'primary_sidebar_end': ['version-switcher'],
	'footer_start': ['copyright'],
	'footer_end': [],
	'switcher': {
		'json_url': f'https://{_docs_domain}/versions.json',
		'version_match': version,
	},
}

html_show_sourcelink = False
html_css_files = ['custom.css']
html_js_files = ['favicon-swap.js']
html_favicon = f'/{release}/_static/favicon.png'

master_doc = 'index'
