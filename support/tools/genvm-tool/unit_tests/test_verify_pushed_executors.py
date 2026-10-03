import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from genvm_tool.git import verify_pushed_executors as verify


class _Printer:
	def __init__(self):
		self.records = []

	def put(self, topic, **fields):
		self.records.append((topic, fields))

	@property
	def text(self):
		return '\n'.join(
			' '.join([topic, *(str(value) for value in fields.values())])
			for topic, fields in self.records
		)


def _git(repo: Path, *args: str, input: str | None = None) -> str:
	result = subprocess.run(
		['git', '-C', str(repo), *args],
		input=input,
		capture_output=True,
		text=True,
		check=True,
	)
	return result.stdout.strip()


def _init_repo(path: Path, bare: bool = False) -> None:
	path.mkdir(parents=True)
	args = ['init']
	if bare:
		args.append('--bare')
	_git(path, *args)
	if not bare:
		_git(path, 'config', 'user.email', 'tests@example.com')
		_git(path, 'config', 'user.name', 'Tests')


def _commit(repo: Path, name: str) -> str:
	(repo / 'content').write_text(name)
	_git(repo, 'add', 'content')
	_git(repo, 'commit', '-m', name)
	return _git(repo, 'rev-parse', 'HEAD')


def _remote(tmp_path: Path) -> tuple[Path, Path, str]:
	bare = tmp_path / 'executor.git'
	seed = tmp_path / 'seed'
	_init_repo(bare, bare=True)
	_init_repo(seed)
	base = _commit(seed, 'base')
	_git(seed, 'remote', 'add', 'origin', str(bare))
	_git(seed, 'push', '-u', 'origin', 'HEAD:main')
	return bare, seed, base


def _manager(tmp_path: Path) -> Path:
	manager = tmp_path / 'manager'
	_init_repo(manager)
	return manager


def _clone_line(manager: Path, remote: Path, version: str) -> Path:
	path = manager / f'executors/v{version}.x'
	path.parent.mkdir(parents=True, exist_ok=True)
	subprocess.run(
		['git', 'clone', '--branch', 'main', str(remote), str(path)],
		capture_output=True,
		text=True,
		check=True,
	)
	_git(path, 'config', 'user.email', 'tests@example.com')
	_git(path, 'config', 'user.name', 'Tests')
	return path


def _manager_commit(
	manager: Path, versions: list[str], gitlinks: dict[str, str]
) -> str:
	(manager / '.genvm-monorepo-root').write_text(
		json.dumps({'active-versions': [f'v{version}' for version in versions]})
	)
	_git(manager, 'add', '.genvm-monorepo-root')
	for version, sha in gitlinks.items():
		_git(
			manager,
			'update-index',
			'--add',
			'--cacheinfo',
			f'160000,{sha},executors/v{version}.x',
		)
	_git(manager, 'commit', '-m', 'manager')
	return _git(manager, 'rev-parse', 'HEAD')


def _run(manager: Path, manager_ref: str | None = None):
	printer = _Printer()
	args = SimpleNamespace(
		manager_ref=manager_ref,
		executor_remote='origin',
	)
	rc = verify.main(SimpleNamespace(root=manager, printer=printer), args)
	return rc, printer


def test_no_active_executor_lines_pass(tmp_path):
	manager = _manager(tmp_path)
	_manager_commit(manager, [], {})
	rc, printer = _run(manager)
	assert rc == 0
	assert printer.records[-1][1]['status'] == 'ok'


def test_tip_and_ancestor_are_published(tmp_path):
	remote, _, base = _remote(tmp_path)
	manager = _manager(tmp_path)
	line = _clone_line(manager, remote, '0.3')
	newer = _commit(line, 'newer')
	_git(line, 'push', 'origin', 'HEAD:main')

	_manager_commit(manager, ['0.2', '0.3'], {'0.2': newer, '0.3': base})
	_clone_line(manager, remote, '0.2')
	rc, _ = _run(manager)
	assert rc == 0


def test_local_only_gitlink_is_rejected(tmp_path):
	remote, _, _ = _remote(tmp_path)
	manager = _manager(tmp_path)
	line = _clone_line(manager, remote, '0.3')
	local = _commit(line, 'local-only')
	_manager_commit(manager, ['0.3'], {'0.3': local})

	rc, printer = _run(manager)
	assert rc == 1
	assert 'executors/v0.3.x' in printer.text
	assert 'absent from executor remote' in printer.text


def test_each_line_is_asked_from_its_own_clone(tmp_path, monkeypatch):
	remote, _, base = _remote(tmp_path)
	manager = _manager(tmp_path)
	line2 = _clone_line(manager, remote, '0.2')
	line3 = _clone_line(manager, remote, '0.3')
	local2 = _commit(line2, 'local-2')
	local3 = _commit(line3, 'local-3')
	_manager_commit(
		manager,
		['0.2', '0.3'],
		{'0.2': local2, '0.3': local3},
	)

	# Both lines are clones of one repository, so they share a remote URL while
	# holding disjoint objects. The question has to be put once per line, from
	# the clone that holds the commit being asked about.
	asked = []
	original = verify._published

	def counted(target, remote):
		asked.append(target.path)
		return original(target, remote)

	monkeypatch.setattr(verify, '_published', counted)
	rc, printer = _run(manager)
	assert rc == 1
	assert asked == [line2, line3]
	assert 'executors/v0.2.x' in printer.text
	assert 'executors/v0.3.x' in printer.text
	assert base not in printer.text


def test_only_unpublished_line_is_named(tmp_path):
	remote, _, base = _remote(tmp_path)
	manager = _manager(tmp_path)
	_clone_line(manager, remote, '0.2')
	line3 = _clone_line(manager, remote, '0.3')
	local = _commit(line3, 'local')
	_manager_commit(manager, ['0.2', '0.3'], {'0.2': base, '0.3': local})

	rc, printer = _run(manager)
	assert rc == 1
	assert 'executors/v0.2.x' not in printer.text
	assert 'executors/v0.3.x' in printer.text


def test_supplied_ref_controls_active_set_and_gitlinks(tmp_path):
	remote, _, base = _remote(tmp_path)
	manager = _manager(tmp_path)
	line = _clone_line(manager, remote, '0.3')
	published_ref = _manager_commit(manager, ['0.3'], {'0.3': base})
	local = _commit(line, 'local')
	_manager_commit(manager, ['0.3'], {'0.3': local})

	assert _run(manager)[0] == 1
	assert _run(manager, published_ref)[0] == 0

	_git(manager, 'checkout', '--detach', published_ref)
	removed_ref = _manager_commit(manager, [], {})
	assert _run(manager, removed_ref)[0] == 0


def test_missing_gitlink_rejects_but_unverifiable_local_state_warns(tmp_path):
	remote, _, base = _remote(tmp_path)

	manager = _manager(tmp_path / 'missing-gitlink')
	_manager_commit(manager, ['0.3'], {})
	rc, printer = _run(manager)
	assert rc == 1
	assert 'malformed gitlink' in printer.text

	manager = _manager(tmp_path / 'missing-checkout')
	_manager_commit(manager, ['0.3'], {'0.3': base})
	rc, printer = _run(manager)
	assert rc == 0
	assert 'checkout is missing' in printer.text
	assert 'WARNING (push allowed)' in printer.text

	manager = _manager(tmp_path / 'missing-remote')
	line = _clone_line(manager, remote, '0.3')
	_git(line, 'remote', 'remove', 'origin')
	_manager_commit(manager, ['0.3'], {'0.3': base})
	rc, printer = _run(manager)
	assert rc == 0
	assert "remote 'origin' is missing" in printer.text
	assert 'WARNING (push allowed)' in printer.text


def test_remote_failure_and_timeout_warn_without_rejecting(tmp_path, monkeypatch):
	remote, _, base = _remote(tmp_path)
	manager = _manager(tmp_path)
	line = _clone_line(manager, remote, '0.3')
	_manager_commit(manager, ['0.3'], {'0.3': base})
	_git(line, 'remote', 'set-url', 'origin', str(tmp_path / 'absent.git'))
	rc, printer = _run(manager)
	assert rc == 0
	assert 'unreachable' in printer.text
	assert 'WARNING (push allowed)' in printer.text

	original = subprocess.run

	def timed_out(*args, **kwargs):
		# Only the negotiation may hang; the local object probe still has to run.
		if 'fetch' in args[0]:
			raise subprocess.TimeoutExpired(args[0], verify.common.GIT_REMOTE_TIMEOUT)
		return original(*args, **kwargs)

	with monkeypatch.context() as patch:
		patch.setattr(verify.subprocess, 'run', timed_out)
		published, problem = verify._published(
			verify._Target('executors/v0.3.x', line, base), 'origin'
		)
	assert published is None
	assert 'timed out' in problem


def test_gitlink_missing_from_the_checkout_warns_without_rejecting(tmp_path):
	remote, seed, _ = _remote(tmp_path)
	manager = _manager(tmp_path)
	_clone_line(manager, remote, '0.3')
	unfetched = _commit(seed, 'remote-only')
	_git(seed, 'push', 'origin', 'HEAD:main')
	_manager_commit(manager, ['0.3'], {'0.3': unfetched})

	rc, printer = _run(manager)
	assert rc == 0
	assert 'absent from this executor checkout' in printer.text
	assert 'WARNING (push allowed)' in printer.text


def test_a_hooks_git_dir_does_not_capture_submodule_queries(tmp_path, monkeypatch):
	# Git exports GIT_DIR (and friends) to hooks, and they outrank `-C`. Left in
	# the environment, every executor query would answer for the manager instead.
	remote, _, base = _remote(tmp_path)
	manager = _manager(tmp_path)
	_clone_line(manager, remote, '0.3')
	_manager_commit(manager, ['0.3'], {'0.3': base})

	monkeypatch.setenv('GIT_DIR', str(manager / '.git'))
	monkeypatch.setenv('GIT_WORK_TREE', str(manager))
	rc, printer = _run(manager)
	assert rc == 0
	assert printer.records[-1][1]['status'] == 'ok'


def test_a_stale_clone_still_recognizes_a_published_gitlink(tmp_path):
	remote, seed, base = _remote(tmp_path)
	manager = _manager(tmp_path)
	_clone_line(manager, remote, '0.3')
	# The remote moves on and the clone never fetches: the gitlink is no longer a
	# tip anywhere the clone knows of, but the remote still holds the commit.
	_commit(seed, 'remote-only')
	_git(seed, 'push', 'origin', 'HEAD:main')
	_manager_commit(manager, ['0.3'], {'0.3': base})

	rc, printer = _run(manager)
	assert rc == 0
	assert printer.records[-1][1]['status'] == 'ok'


def test_defaults_to_hook_ref_then_head(tmp_path, monkeypatch):
	remote, _, base = _remote(tmp_path)
	manager = _manager(tmp_path)
	line = _clone_line(manager, remote, '0.3')
	published_ref = _manager_commit(manager, ['0.3'], {'0.3': base})
	local = _commit(line, 'local')
	_manager_commit(manager, ['0.3'], {'0.3': local})

	monkeypatch.setenv('PRE_COMMIT_TO_REF', published_ref)
	assert _run(manager)[0] == 0
	monkeypatch.delenv('PRE_COMMIT_TO_REF')
	assert _run(manager)[0] == 1


_ZERO = '0' * 40


def test_pinned_pre_commit_exposes_only_first_push_update(tmp_path):
	repo = tmp_path / 'pre-commit'
	_init_repo(repo)
	base = _commit(repo, 'base')
	first = _commit(repo, 'first')
	second = _commit(repo, 'second')
	_git(repo, 'update-ref', 'refs/remotes/origin/main', base)
	capture = repo / 'captured'
	(repo / 'capture.py').write_text(
		'import os\n'
		'from pathlib import Path\n'
		"Path(os.environ['CAPTURE_FILE']).write_text("
		"os.environ.get('PRE_COMMIT_TO_REF', 'missing'))\n"
	)
	(repo / '.pre-commit-config.yaml').write_text(
		'repos:\n'
		'- repo: local\n'
		'  hooks:\n'
		'  - id: capture-ref\n'
		'    name: capture-ref\n'
		'    entry: python capture.py\n'
		'    language: system\n'
		'    stages: [pre-push]\n'
		'    always_run: true\n'
		'    pass_filenames: false\n'
	)
	env = {
		**os.environ,
		'CAPTURE_FILE': str(capture),
		'PRE_COMMIT_HOME': str(tmp_path / 'pre-commit-cache'),
	}
	subprocess.run(
		['pre-commit', 'install', '--hook-type', 'pre-push'],
		cwd=repo,
		capture_output=True,
		text=True,
		env=env,
		check=True,
	)
	hook = Path(_git(repo, 'rev-parse', '--git-path', 'hooks/pre-push'))
	stdin = (
		f'refs/heads/first {first} refs/heads/first {_ZERO}\n'
		f'refs/heads/second {second} refs/heads/second {_ZERO}\n'
	)
	subprocess.run(
		[hook, 'origin', 'unused'],
		cwd=repo,
		input=stdin,
		capture_output=True,
		text=True,
		env=env,
		check=True,
	)
	assert capture.read_text() == first
