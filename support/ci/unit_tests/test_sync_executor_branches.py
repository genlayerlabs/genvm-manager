from types import SimpleNamespace

from tools import sync_executor_branches as sync


def test_target_branch_uses_declared_release_branch():
	assert sync.target_branch('v0.6', 'v0.3.x') == 'v0.3.x'


def test_target_branch_derives_dev_branch():
	assert sync.target_branch('v0.6-dev', 'v0.3.x') == 'v0.3-dev'


def test_sync_line_pushes_without_force(monkeypatch):
	commands = []

	def git(*args, check=True):
		commands.append((args, check))
		if args[:4] == ('config', '-f', '.gitmodules', '--get'):
			return SimpleNamespace(returncode=0, stdout='v0.3.x\n', stderr='')
		if args[:3] == ('-C', 'executors/v0.3.x', 'rev-parse'):
			return SimpleNamespace(returncode=0, stdout=f'{"a" * 40}\n', stderr='')
		return SimpleNamespace(returncode=0, stdout='', stderr='')

	monkeypatch.setattr(sync, 'git', git)
	target, sha = sync.sync_line('v0.3', 'v0.6-dev')

	assert target == 'v0.3-dev'
	assert sha == 'a' * 40
	push = commands[-1][0]
	assert push == (
		'-C',
		'executors/v0.3.x',
		'push',
		'origin',
		f'{"a" * 40}:refs/heads/v0.3-dev',
	)
	assert '--force' not in push


def test_all_lines_are_attempted_before_failures_are_reported(monkeypatch, capsys):
	attempted = []

	def sync_line(line, manager_branch):
		attempted.append((line, manager_branch))
		if line == 'v0.2':
			raise sync.SyncError('non-fast-forward')
		return f'{line}-dev', line * 8

	monkeypatch.setattr(sync, 'active_lines', lambda: ['v0.2', 'v0.3', 'v0.4'])
	monkeypatch.setattr(sync, 'sync_line', sync_line)

	assert sync.sync_executor_branches('v0.6-dev') == 1
	assert attempted == [
		('v0.2', 'v0.6-dev'),
		('v0.3', 'v0.6-dev'),
		('v0.4', 'v0.6-dev'),
	]
	output = capsys.readouterr().out
	assert '1 executor branch update(s) failed' in output
	assert '- v0.2: non-fast-forward' in output
