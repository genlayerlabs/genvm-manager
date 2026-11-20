import json

import pytest
from genvm_tool import cmd_configure, common

CONFIG = {
	'executor-lines': {
		'v0.10': {'status': 'preview'},
		'v0.4': {'status': 'current'},
		'v0.9': {'status': 'maintained'},
		'v0.2': {'status': 'replay-only'},
	}
}


def test_versions_are_ordered_by_version():
	assert common.active_versions_from_config(CONFIG) == ['0.2', '0.4', '0.9', '0.10']


def test_versions_read_a_commit_predating_statuses():
	legacy = {'active-versions': ['v0.4', 'v0.3', 'v0.2']}
	assert common.active_versions_from_config(legacy) == ['0.2', '0.3', '0.4']


def test_current_line():
	assert common.current_line_from_config(CONFIG) == 'v0.4'


@pytest.mark.parametrize(
	('statuses', 'found'),
	[(['maintained'], 'none'), (['current', 'current'], 'v0.0, v0.1'), ([], 'none')],
)
def test_current_line_must_be_unique(statuses, found):
	config = {
		'executor-lines': {f'v0.{i}': {'status': s} for i, s in enumerate(statuses)}
	}
	with pytest.raises(common.ToolError, match=f'found {found}$'):
		common.current_line_from_config(config)


def test_configure_takes_the_current_line_as_primary(tmp_path):
	for line in CONFIG['executor-lines']:
		manifest = tmp_path / 'executors' / f'{line}.x' / 'manifest.json'
		manifest.parent.mkdir(parents=True)
		manifest.write_text(json.dumps({'executor-version': f'{line}.7'}))
	info = cmd_configure.build_independent_info({'version': 'v0.6.0', **CONFIG}, tmp_path)
	assert info['primary_executor_version'] == 'v0.4.7'
	assert list(info['executor_versions']) == list(CONFIG['executor-lines'])
