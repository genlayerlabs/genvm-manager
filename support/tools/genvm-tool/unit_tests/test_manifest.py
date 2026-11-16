"""What build-manifest hands the manager, and that post-install reads it back."""

import importlib.util
import json
from pathlib import Path

import pytest
from genvm_tool import chainforks, common, manifest

# post-install's own YAML reader is not a package; the generated file is
# checked against the parser that consumes it
MICRO_YAML = (
	common.find_root(Path(__file__).parent)
	/ 'install/lib/python/post-install/micro_yaml.py'
)

RUNNER_FORKS = ['genvm/executor/v0.2', 'genvm/executor/v0.3']

BASE = 'runners_download_urls:\n  - "https://example.invalid/{name}/{hash}.{ext}"\n'

SCHEDULE = {
	'forks': {'genvm/executor/v0.3': {}, 'genvm/executor/v0.2': {}},
	'networks': {
		'dev': {'forks': {'genvm/executor/v0.3': 'always', 'genvm/executor/v0.2': 'always'}}
	},
}


def _micro_yaml_loads():
	spec = importlib.util.spec_from_file_location('micro_yaml', MICRO_YAML)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module.loads


@pytest.fixture
def root(tmp_path):
	"""Two active lines."""
	tmp_path.joinpath('.genvm-monorepo-root').write_text(
		json.dumps(
			{
				'executor-lines': {
					'v0.3': {'status': 'current'},
					'v0.2': {'status': 'replay-only'},
				}
			}
		)
	)
	base = tmp_path / manifest.BASE_REL
	base.parent.mkdir(parents=True)
	base.write_text(BASE)
	schedule = tmp_path / chainforks.REL
	schedule.parent.mkdir(parents=True)
	schedule.write_text(json.dumps(SCHEDULE))
	_line(tmp_path, '0.3')
	_line(tmp_path, '0.2')
	return tmp_path


def _line(root: Path, line: str, fork: str | None = None) -> None:
	path = root / f'executors/v{line}.x/manifest.json'
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(
		json.dumps(
			{
				'executor-version': f'v{line}.0',
				'fork': fork or f'genvm/executor/v{line}',
			}
		)
	)


def test_every_active_line_is_listed_with_its_fork(root):
	versions = manifest.build(root, [])['executor_versions']

	assert sorted(versions) == ['v0.2.0', 'v0.3.0']
	assert dict(versions['v0.3.0']) == {'fork': 'genvm/executor/v0.3'}


def test_the_written_file_reads_back_through_post_install(root, tmp_path):
	out = tmp_path / 'data/manifest.yaml'
	manifest.write(root, out, RUNNER_FORKS)

	doc = _micro_yaml_loads()(out.read_text())

	assert doc['executor_versions']['v0.3.0']['fork'] == 'genvm/executor/v0.3'
	assert doc['executor_versions']['v0.2.0']['fork'] == 'genvm/executor/v0.2'
	assert doc['runners_download_urls'] == ['https://example.invalid/{name}/{hash}.{ext}']
	assert doc['runner_forks'] == RUNNER_FORKS


def test_runner_forks_are_listed_sorted_and_unique(root):
	forks = manifest.build(
		root, ['genvm/executor/v0.3', 'genvm/executor/v0.2', 'genvm/executor/v0.3']
	)

	assert list(forks['runner_forks']) == ['genvm/executor/v0.2', 'genvm/executor/v0.3']


def test_a_runner_fork_missing_from_the_schedule_fails_the_build(root):
	with pytest.raises(chainforks.Error, match='genvm/executor/v0.3/typo'):
		manifest.build(root, ['genvm/executor/v0.3/typo'])


def test_a_line_fork_missing_from_the_schedule_fails_the_build(root):
	_line(root, '0.3', 'genvm/executor/typo')

	with pytest.raises(chainforks.Error, match='genvm/executor/typo'):
		manifest.build(root, [])


def test_a_broken_schedule_fails_the_build(root):
	(root / chainforks.REL).write_text(json.dumps({**SCHEDULE, 'extra': {}}))

	with pytest.raises(chainforks.Error, match='unknown keys'):
		manifest.build(root, [])


def test_the_shipped_schedule_is_valid():
	chainforks.load(common.find_root(Path(__file__).parent))
