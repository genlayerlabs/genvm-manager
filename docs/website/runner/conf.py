import json
import os
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'common'))

import conf_common  # noqa: E402

# Set per page by runner_pages.py, which builds every page in one process
_page = json.loads(os.environ['GENVM_RUNNER_PAGE'])

# Left empty: it lands in `_static`, which every runner page shares
release = ''
version = ''

conf_common.apply(
	globals(),
	kind='runner',
	title=_page['id'],
	crumbs=[
		(f'Manager {conf_common.MONOREPO["version"]}', conf_common.MANAGER_PATH),
		(f'Runner {_page["id"]}', conf_common.runner_path(_page['id'], _page['hash32'])),
	],
)

html_theme_options['secondary_sidebar_items'] = ['page-toc']  # noqa: F821
html_copy_source = False

extensions = ['sphinx.ext.intersphinx']

master_doc = 'index'
exclude_patterns = ['_build']

# Docstrings link to guides and the spec of the executor that ships the runner
_executor_path = conf_common.executor_path(_page['executor_version'])
intersphinx_mapping = {
	'executor': (
		conf_common.relative('runner', _executor_path),
		conf_common.inventory(_executor_path),
	),
}
intersphinx_disabled_reftypes: list[str] = []

# A runner with reference docs may tune sphinx next to them, in `docs/conf.py`
if _page['docs_conf'] is not None:
	runpy.run_path(_page['docs_conf'])['configure'](globals())
