import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'common'))

import conf_common  # noqa: E402

# The specification lives in the executor line that implements it; this site
# documents how the manager runs it. Cross-links to the current line's site go
# through intersphinx.
release = conf_common.MONOREPO['version']
version = release

_line = conf_common.current_line()
_executor_version = conf_common.executor_version(_line)

conf_common.apply(
	globals(),
	kind='manager',
	title='GenVM',
	crumbs=[
		(f'Manager {release}', conf_common.MANAGER_PATH),
		(
			f'Executor {_executor_version}',
			conf_common.executor_path(_executor_version),
		),
	],
)

extensions = [
	'sphinx.ext.intersphinx',
	'sphinxcontrib.mermaid',
	'sphinxcontrib.openapi',
	'myst_parser',
]

exclude_patterns = [
	'_build',
	'Thumbs.db',
	'.DS_Store',
	'*_generated.rst',
	'api',
	'impl-spec/appendix/runners-versions.rst',
]

mermaid_version = '11.6.0'
mermaid_output_format = 'raw'

# The sites reference each other, so the pipeline builds them twice; on the
# first pass an inventory may be missing
_executor_path = conf_common.executor_path(_executor_version)
intersphinx_mapping = {
	'executor': (
		conf_common.relative('manager', _executor_path),
		conf_common.inventory(_executor_path),
	),
}
# Unresolved :doc: references fall through to the executor site too
intersphinx_disabled_reftypes: list[str] = []

master_doc = 'index'
