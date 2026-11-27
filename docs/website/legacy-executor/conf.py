import json
import os
import runpy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'common'))

import conf_common  # noqa: E402

# Builds an executor line whose own `conf.py` predates `conf_common`: its
# settings, with the site-wide ones on top
_line = json.loads(os.environ['GENVM_LEGACY_EXECUTOR'])
globals().update(
	(k, v) for k, v in runpy.run_path(_line['conf']).items() if not k.startswith('__')
)

conf_common.apply(
	globals(),
	kind='executor',
	title='GenVM Executor',
	crumbs=[
		(f'Manager {conf_common.MONOREPO["version"]}', conf_common.MANAGER_PATH),
		(f'Executor {release}', conf_common.executor_path(release)),  # noqa: F821
	],
)

if 'sphinx.ext.intersphinx' not in extensions:  # noqa: F821
	extensions.append('sphinx.ext.intersphinx')  # noqa: F821

# Its `genvm` references point at the spec, which the current line's site holds
_spec_path = conf_common.executor_path(
	conf_common.executor_version(conf_common.current_line())
)
intersphinx_mapping = {
	**globals().get('intersphinx_mapping', {}),
	'genvm': (
		conf_common.relative('executor', _spec_path),
		conf_common.inventory(_spec_path),
	),
}
