"""What build-manifest hands the manager, and that post-install reads it back."""

import importlib.util
import json
from pathlib import Path

import pytest
from genvm_tool import common, manifest

# post-install's own YAML reader is not a package; the generated file is
# checked against the parser that consumes it
MICRO_YAML = (
	common.find_root(Path(__file__).parent)
	/ 'install/lib/python/post-install/micro_yaml.py'
)

BASE = 'runners_download_urls:\n  - "https://example.invalid/{name}/{hash}.{ext}"\n'


def _micro_yaml_loads():
	spec = importlib.util.spec_from_file_location('micro_yaml', MICRO_YAML)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module.loads


@pytest.fixture
def root(tmp_path):
	"""Two active lines."""
	tmp_path.joinpath('.genvm-monorepo-root').write_text(
		json.dumps({'active-versions': ['v0.3', 'v0.2']})
	)
	base = tmp_path / manifest.BASE_REL
	base.parent.mkdir(parents=True)
	base.write_text(BASE)
	_line(tmp_path, '0.3')
	_line(tmp_path, '0.2')
	return tmp_path


def _line(root: Path, line: str) -> None:
	path = root / f'executors/v{line}.x/manifest.json'
	path.parent.mkdir(parents=True)
	path.write_text(
		json.dumps(
			{
				'executor-version': f'v{line}.0',
				'available-after': '2024-09-01T00:00:00Z',
			}
		)
	)


def test_every_active_line_is_listed_with_its_availability(root):
	versions = manifest.build(root)['executor_versions']

	assert sorted(versions) == ['v0.2.0', 'v0.3.0']
	assert dict(versions['v0.3.0']) == {'available_after': '2024-09-01T00:00:00Z'}


def test_the_written_file_reads_back_through_post_install(root, tmp_path):
	out = tmp_path / 'data/manifest.yaml'
	manifest.write(root, out)

	doc = _micro_yaml_loads()(out.read_text())

	assert doc['executor_versions']['v0.3.0']['available_after'] == '2024-09-01T00:00:00Z'
	assert doc['executor_versions']['v0.2.0']['available_after'] == '2024-09-01T00:00:00Z'
	assert doc['runners_download_urls'] == ['https://example.invalid/{name}/{hash}.{ext}']
