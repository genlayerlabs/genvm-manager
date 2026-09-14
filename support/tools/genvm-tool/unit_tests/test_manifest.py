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
PINS = {'arm64-macos': 'b' * 64, 'amd64-linux': 'A' * 64}
# what build-manifest emits for PINS: sorted, lowercased
WRITTEN = {'amd64-linux': 'a' * 64, 'arm64-macos': 'b' * 64}


def _micro_yaml_loads():
	spec = importlib.util.spec_from_file_location('micro_yaml', MICRO_YAML)
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module.loads


@pytest.fixture
def root(tmp_path):
	"""Two active lines; v0.3 pins its tarballs, v0.2 does not."""
	tmp_path.joinpath('.genvm-monorepo-root').write_text(
		json.dumps({'active-versions': ['v0.3', 'v0.2']})
	)
	base = tmp_path / manifest.BASE_REL
	base.parent.mkdir(parents=True)
	base.write_text(BASE)
	_line(tmp_path, '0.3', {'executor-sha256': PINS})
	_line(tmp_path, '0.2', {})
	return tmp_path


def _line(root: Path, line: str, extra: dict) -> None:
	path = root / f'executors/v{line}.x/manifest.json'
	path.parent.mkdir(parents=True)
	path.write_text(
		json.dumps(
			{
				'executor-version': f'v{line}.0',
				'available-after': '2024-09-01T00:00:00Z',
				**extra,
			}
		)
	)


def test_pins_are_copied_per_platform_sorted_and_lowercased(root):
	entry = manifest.build(root)['executor_versions']['v0.3.0']

	assert list(entry['sha256']) == ['amd64-linux', 'arm64-macos']
	assert dict(entry['sha256']) == WRITTEN


@pytest.mark.parametrize(
	'pins',
	[{'x86_64-linux': 'a' * 64}, {'amd64-linux': 'a' * 63}, {'amd64-linux': 'g' * 64}],
	ids=['unknown-platform', 'short', 'not-hex'],
)
def test_a_malformed_pin_fails_the_build(root, pins):
	path = root / 'executors/v0.3.x/manifest.json'
	line = json.loads(path.read_text())
	line['executor-sha256'] = pins
	path.write_text(json.dumps(line))

	with pytest.raises(common.ToolError, match='executors/v0.3.x/manifest.json'):
		manifest.build(root)


def test_a_line_without_pins_gets_no_sha256_key(root):
	entry = manifest.build(root)['executor_versions']['v0.2.0']

	assert dict(entry) == {'available_after': '2024-09-01T00:00:00Z'}


def test_the_written_file_reads_back_through_post_install(root, tmp_path):
	out = tmp_path / 'data/manifest.yaml'
	manifest.write(root, out)

	doc = _micro_yaml_loads()(out.read_text())

	assert doc['executor_versions']['v0.3.0']['sha256'] == WRITTEN
	assert doc['executor_versions']['v0.3.0']['available_after'] == '2024-09-01T00:00:00Z'
	assert 'sha256' not in doc['executor_versions']['v0.2.0']
	assert doc['runners_download_urls'] == ['https://example.invalid/{name}/{hash}.{ext}']
