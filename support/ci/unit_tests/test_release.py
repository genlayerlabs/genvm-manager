import argparse
import json

import pytest

from pipelines import release


@pytest.fixture
def monorepo(tmp_path, monkeypatch):
	path = tmp_path / '.genvm-monorepo-root'
	path.write_text(
		json.dumps(
			{
				'version': 'v0.6.0-rc2',
				'active-versions': ['v0.3', 'v0.2'],
				'support-only-versions': ['v0.2'],
				'artifacts_dir': 'build/test-artifacts',
			},
			indent=4,
		)
		+ '\n'
	)
	monkeypatch.setattr(release.ci_lib, 'ROOT_DIR', tmp_path)
	monkeypatch.setattr(release, 'release_filter_tag', lambda: 'integration & stable')
	return path


def run(command, override=None):
	parser = argparse.ArgumentParser()
	command.add_to(parser)
	argv = [] if override is None else ['--version-override', override]
	return command.handler(parser.parse_args(argv))


@pytest.mark.parametrize('override', [None, '', 'v0.6.0-rc2'])
def test_apply_unchanged_version_does_not_write(monorepo, override):
	before = monorepo.read_bytes()
	mtime = monorepo.stat().st_mtime_ns
	assert run(release.ApplyReleaseVersion(), override) == 0
	assert monorepo.read_bytes() == before
	assert monorepo.stat().st_mtime_ns == mtime


@pytest.mark.parametrize('override', ['v1.2.3', 'v0.6.0-rc3', 'v1.2.3+build.4'])
def test_apply_changes_only_version_and_preserves_format(monorepo, override):
	before = monorepo.read_text()
	assert run(release.ApplyReleaseVersion(), override) == 0
	assert monorepo.read_text() == before.replace('v0.6.0-rc2', override)


@pytest.mark.parametrize(
	'override',
	[
		'1.2.3',
		'v1.2',
		'release',
		' v1.2.3',
		'v1.2.3 rc',
		'v1.2.3;x',
		'v1.2.3-rc..1',
		'v1.2.3-rc.',
		'v1.2.3-x.lock',
	],
)
def test_plan_and_apply_reject_invalid_version(
	monorepo, tmp_path, monkeypatch, override
):
	before = monorepo.read_bytes()
	output = tmp_path / 'github-output'
	monkeypatch.setenv('GITHUB_OUTPUT', str(output))
	assert run(release.PlanReleaseMatrix(), override) == 1
	assert run(release.ApplyReleaseVersion(), override) == 1
	assert monorepo.read_bytes() == before
	assert not output.exists()


@pytest.mark.parametrize('override', ['', 'v1.2.3-rc1'])
def test_plan_tags_with_override(monorepo, tmp_path, monkeypatch, override):
	output = tmp_path / 'github-output'
	monkeypatch.setenv('GITHUB_OUTPUT', str(output))
	assert run(release.PlanReleaseMatrix(), override) == 0
	outputs = dict(line.split('=', 1) for line in output.read_text().splitlines())
	assert outputs['tag'] == (override or 'v0.6.0-rc2')
