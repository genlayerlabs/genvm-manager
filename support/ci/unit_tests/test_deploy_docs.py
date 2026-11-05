import argparse
import json
import subprocess

import pytest

from tools import deploy_docs as deploy


@pytest.fixture
def publication(tmp_path, monkeypatch):
	run = subprocess.run

	def git(path, *args):
		return run(
			['git', '-C', str(path), *args], check=True, capture_output=True, text=True
		).stdout.strip()

	remote = tmp_path / 'remote.git'
	run(['git', 'init', '--bare', '--initial-branch=main', str(remote)], check=True)
	site = tmp_path / 'site'
	run(['git', 'clone', str(remote), str(site)], check=True)
	git(site, 'config', 'user.email', 'test@example.com')
	git(site, 'config', 'user.name', 'Test')
	(site / '_site' / 'v0.6.x').mkdir(parents=True)
	(site / '_site' / 'v0.6.x' / 'obsolete.html').write_text('old')
	(site / '_site' / 'versions.json').write_text(
		json.dumps([deploy.entry_for('main', True)])
	)
	git(site, 'add', '.')
	git(site, 'commit', '-m', 'Initial site')
	git(site, 'push', 'origin', 'main')
	html = tmp_path / 'html'
	html.mkdir()
	(html / 'index.html').write_text('new docs')
	(html / '.doctrees').mkdir()
	(html / '.doctrees' / 'cache').write_text('cache')
	parser = argparse.ArgumentParser()
	tool = deploy.DeployDocs()
	tool.add_to(parser)
	args = parser.parse_args(
		['--site', str(site), '--html', str(html), '--version', 'v0.6.x', '--push']
	)
	monkeypatch.setattr(deploy.time, 'sleep', lambda _: None)
	return tool, args, git, remote, site, run


def test_publish_replaces_tree_and_skips_unchanged(publication):
	tool, args, git, remote, site, _ = publication
	assert tool.handler(args) == 0
	head = git(remote, 'rev-parse', 'main')
	assert (site / '_site' / args.version / 'index.html').read_text() == 'new docs'
	assert not (site / '_site' / args.version / 'obsolete.html').exists()
	assert not (site / '_site' / args.version / '.doctrees').exists()
	assert tool.handler(args) == 0
	assert git(remote, 'rev-parse', 'main') == head


@pytest.mark.parametrize('preferred', [False, True])
def test_publish_retries_concurrent_version_update(
	publication, monkeypatch, tmp_path, preferred
):
	tool, args, git, remote, site, run = publication
	args.preferred = preferred
	competitor = tmp_path / 'competitor'
	run(['git', 'clone', str(remote), str(competitor)], check=True)
	git(competitor, 'config', 'user.email', 'other@example.com')
	git(competitor, 'config', 'user.name', 'Other')
	pushes = 0

	def race(command, **kwargs):
		nonlocal pushes
		if command[:2] == ['git', 'push']:
			pushes += 1
			if pushes == 1:
				(competitor / '_site' / 'v0.6-dev').mkdir()
				(competitor / '_site' / 'v0.6-dev' / 'index.html').write_text('concurrent docs')
				deploy.update_versions(competitor / '_site' / 'versions.json', 'v0.6-dev', True)
				git(competitor, 'add', '.')
				git(competitor, 'commit', '-m', 'Concurrent version')
				git(competitor, 'push', 'origin', 'main')
		return run(command, **kwargs)

	monkeypatch.setattr(deploy.subprocess, 'run', race)
	assert tool.handler(args) == 0
	assert pushes == 2
	assert git(remote, 'show', 'main:_site/v0.6-dev/index.html') == 'concurrent docs'
	assert git(remote, 'show', 'main:_site/v0.6.x/index.html') == 'new docs'
	versions = json.loads(git(remote, 'show', 'main:_site/versions.json'))
	assert {entry['version'] for entry in versions} == {'main', 'v0.6-dev', 'v0.6.x'}
	assert [entry['version'] for entry in versions if entry['preferred']] == [
		'v0.6.x' if preferred else 'v0.6-dev'
	]


def test_publish_stops_after_repeated_rejection(publication, monkeypatch):
	tool, args, git, remote, _, run = publication
	head = git(remote, 'rev-parse', 'main')
	pushes = 0

	def reject(command, **kwargs):
		nonlocal pushes
		if command[:2] == ['git', 'push']:
			pushes += 1
			raise subprocess.CalledProcessError(1, command)
		return run(command, **kwargs)

	monkeypatch.setattr(deploy.subprocess, 'run', reject)
	assert tool.handler(args) == 1
	assert pushes == 5
	assert git(remote, 'rev-parse', 'main') == head


def test_publish_refuses_dirty_checkout(publication):
	tool, args, git, remote, site, _ = publication
	head = git(remote, 'rev-parse', 'main')
	(site / 'keep.txt').write_text('local work')
	assert tool.handler(args) == 1
	assert (site / 'keep.txt').read_text() == 'local work'
	assert git(remote, 'rev-parse', 'main') == head


def test_stage_does_not_push(publication):
	tool, args, git, remote, site, _ = publication
	head = git(remote, 'rev-parse', 'main')
	args.push = False
	assert tool.handler(args) == 0
	assert (site / '_site' / args.version / 'index.html').exists()
	assert git(remote, 'rev-parse', 'main') == head
