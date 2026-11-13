import argparse
import json

import pytest

from tools import versions

ROOT_CONF = {
	'$schema': './support/schemas/genvm-monorepo-root.json',
	'version': 'v0.6.0-rc13',
	'executor-lines': {
		'v0.10': {'status': 'preview'},
		'v0.4': {'status': 'current'},
		'v0.9': {'status': 'maintained'},
		'v0.2': {'status': 'replay-only'},
	},
	'artifacts_dir': 'build/test-artifacts',
}


def write_root(path, conf):
	(path / '.genvm-monorepo-root').write_text(json.dumps(conf, indent=4) + '\n')


@pytest.fixture
def root(tmp_path, monkeypatch):
	write_root(tmp_path, ROOT_CONF)
	(tmp_path / 'implementation').mkdir()
	(tmp_path / 'implementation' / 'Cargo.toml').write_text(
		'[package]\nname = "genvm-manager"\nversion = "0.6.3"\n\n[dependencies]\nfoo = { version = "1.2.3" }\n'
	)
	monkeypatch.setattr(versions.ci_lib, 'ROOT_DIR', tmp_path)
	monkeypatch.delenv('MONOREPO_ROOT', raising=False)
	return tmp_path


def bump(*argv):
	command = versions.CheckVersions()
	parser = argparse.ArgumentParser()
	command.add_to(parser)
	return command.handler(parser.parse_args(['bump', *argv]))


@pytest.mark.parametrize(
	('argv', 'mm'),
	[((), '0.7'), (('minor',), '0.7'), (('major',), '1.0'), (('--set', '0.9'), '0.9')],
)
def test_bump_moves_only_the_manager_train(root, capsys, argv, mm):
	assert bump(*argv) == 0
	assert capsys.readouterr().out == f'{mm}\n'
	conf = json.loads((root / '.genvm-monorepo-root').read_text())
	assert conf == {**ROOT_CONF, 'version': f'v{mm}.0-rc0'}
	assert list(conf) == list(ROOT_CONF)
	cargo = (root / 'implementation' / 'Cargo.toml').read_text()
	assert f'version = "{mm}.0"\n' in cargo
	assert 'foo = { version = "1.2.3" }' in cargo


@pytest.mark.parametrize('target', ['0.6', 'v0.6', '0.5', '0.6.1', 'x'])
def test_bump_rejects_a_train_not_above_the_current(root, target):
	before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
	assert bump('--set', target) != 0
	assert {p: p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


def run_check(*argv):
	command = versions.CheckVersions()
	parser = argparse.ArgumentParser()
	command.add_to(parser)
	return command.handler(parser.parse_args(['lines', *argv]))


def test_lines_are_ordered_by_version(root):
	assert versions.executor_lines() == {
		'v0.2': 'replay-only',
		'v0.4': 'current',
		'v0.9': 'maintained',
		'v0.10': 'preview',
	}
	assert versions.active_versions() == ['0.2', '0.4', '0.9', '0.10']
	assert versions.active_lines() == ['v0.2', 'v0.4', 'v0.9', 'v0.10']
	assert versions.current_line() == 'v0.4'
	assert run_check() == 0


@pytest.mark.parametrize(
	('lines', 'reason'),
	[
		({'v0.4': {'status': 'maintained'}}, 'exactly 1 `current` line, found none'),
		(
			{'v0.4': {'status': 'current'}, 'v0.3': {'status': 'current'}},
			'exactly 1 `current` line, found v0.3, v0.4',
		),
		(
			{'v0.4': {'status': 'current'}, 'v0.3': {'status': 'support-only'}},
			'v0.3: unknown status `support-only`',
		),
		({'v0.4': {'status': 'current'}, '0.3': {'status': 'maintained'}}, '`0.3`'),
		({}, 'exactly 1 `current` line, found none'),
		({'v0.4': 'current'}, 'v0.4: must be an object with a `status`'),
		(['v0.4'], '`executor-lines` must be an object'),
	],
)
def test_check_lines_rejects(root, capsys, lines, reason):
	write_root(root, {**ROOT_CONF, 'executor-lines': lines})
	assert run_check() == 1
	assert reason in capsys.readouterr().out


def test_check_lines_rejects_a_missing_key(root, capsys):
	write_root(root, {'version': 'v0.6.0'})
	assert run_check() == 1
	assert '`executor-lines` must be an object' in capsys.readouterr().out


def test_bump_without_a_cargo_version_writes_nothing(root):
	(root / 'implementation' / 'Cargo.toml').write_text('[package]\nname = "x"\n')
	before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
	assert bump() == 1
	assert {p: p.read_bytes() for p in root.rglob('*') if p.is_file()} == before
