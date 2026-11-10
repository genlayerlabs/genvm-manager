import subprocess
import sys
from pathlib import Path

import ci_lib
import pytest

SCRIPT = ci_lib.ROOT_DIR / 'support' / 'scripts' / 'remove-schema.py'

TAG = 'v0.6.0-rc12'
URL = f'https://raw.githubusercontent.com/genlayerlabs/genvm-manager/refs/tags/{TAG}/docs/schemas'


def _patch(path: Path) -> subprocess.CompletedProcess:
	return subprocess.run(
		[sys.executable, str(SCRIPT), '--tag', TAG, str(path)],
		capture_output=True,
		text=True,
	)


def test_relative_json_schema_becomes_a_tagged_url(tmp_path):
	data = tmp_path / 'data'
	data.mkdir()
	f = data / 'genvm-chainforks.json'
	f.write_text(
		'{\n  "$schema": "../../docs/schemas/genvm-chainforks.json",\n  "forks": {}\n}\n'
	)
	assert _patch(tmp_path).returncode == 0
	assert f.read_text() == (
		f'{{\n  "$schema": "{URL}/genvm-chainforks.json",\n  "forks": {{}}\n}}\n'
	)


def test_absolute_json_schema_is_kept(tmp_path):
	f = tmp_path / '.luarc.json'
	text = '{\n  "$schema": "https://example.com/schema.json"\n}\n'
	f.write_text(text)
	assert _patch(tmp_path).returncode == 0
	assert f.read_text() == text


@pytest.mark.parametrize('schema', ['../schema.json', './docs/schemas/x.json'])
def test_relative_json_schema_outside_docs_schemas_is_rejected(tmp_path, schema):
	f = tmp_path / 'x.json'
	f.write_text(f'{{"$schema": "{schema}"}}\n')
	res = _patch(tmp_path)
	assert res.returncode != 0
	assert str(f) in res.stdout
