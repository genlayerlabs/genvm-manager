import argparse
import subprocess

import ci_lib
import pytest
from pipelines import checks


class Recorder:
	"""Stand-in for ci_lib.run: records commands and replays canned exit codes."""

	def __init__(self, codes: dict[str, int] | None = None):
		self.commands: list[list[str]] = []
		self.codes = codes or {}

	def __call__(self, command, **kwargs) -> subprocess.CompletedProcess:
		command = [str(arg) for arg in command]
		self.commands.append(command)
		code = next((c for k, c in self.codes.items() if k in command), 0)
		if code != 0 and kwargs.get('check', True):
			raise subprocess.CalledProcessError(code, command)
		return subprocess.CompletedProcess(command, code)


@pytest.fixture
def clippy(monkeypatch):
	recorder = Recorder()
	monkeypatch.setattr(ci_lib, 'run', recorder)
	monkeypatch.setattr(ci_lib, 'output', lambda _command: '')
	return recorder


def run() -> int:
	return checks.CargoClippy().handler(argparse.Namespace())


def test_clean_tree_skips_the_fix_run(clippy):
	assert run() == 0
	assert not any('cargo/clippy/fix' in c for c in clippy.commands)


def test_failure_reports_the_fix_patch(monkeypatch, clippy, capsys):
	clippy.codes = {'cargo/clippy': 1}
	monkeypatch.setattr(
		ci_lib, 'output', lambda command: '' if '--name-only' in command else 'a patch'
	)
	summary = []
	monkeypatch.setattr(ci_lib, 'github_step_summary', summary.append)

	assert run() == 1
	assert any('cargo/clippy/fix' in c for c in clippy.commands)
	assert 'a patch' in capsys.readouterr().out
	assert '```diff\na patch```' in summary[0]


def test_failure_without_a_patch_says_so(monkeypatch, clippy, capsys):
	clippy.codes = {'cargo/clippy': 1}
	monkeypatch.setattr(
		ci_lib, 'github_step_summary', lambda _text: pytest.fail('no patch')
	)

	assert run() == 1
	assert 'rewrote nothing' in capsys.readouterr().out


def test_pre_existing_changes_are_excluded_from_summary(monkeypatch, clippy, capsys):
	clippy.codes = {'cargo/clippy': 1}
	monkeypatch.setattr(ci_lib, 'output', lambda _command: 'src/lib.rs')
	summary = []
	monkeypatch.setattr(ci_lib, 'github_step_summary', summary.append)

	assert run() == 1
	assert 'already dirty' in capsys.readouterr().out
	assert len(summary) == 1
	assert 'Patch omitted' in summary[0]
	assert 'src/lib.rs' not in summary[0]
	assert '```diff' not in summary[0]


def test_summary_patch_is_cut_on_a_line_boundary(monkeypatch):
	monkeypatch.setattr(checks, 'SUMMARY_DIFF_LIMIT', 10)
	patch = checks._summary_patch('+one\n+two\n+three\n')
	assert '+one\n+two' in patch
	assert '+three' not in patch
	assert 'truncated' in patch


def test_summary_patch_is_cut_on_bytes(monkeypatch):
	monkeypatch.setattr(checks, 'SUMMARY_DIFF_LIMIT', 10)
	# Five 2-byte characters already fill the limit, so nothing but the first
	# line survives a cut that would land mid-character.
	patch = checks._summary_patch('+ééééé\n+tail\n')
	assert '+tail' not in patch
	assert 'truncated' in patch


def test_step_summary_is_a_noop_outside_actions(monkeypatch):
	monkeypatch.delenv('GITHUB_STEP_SUMMARY', raising=False)
	ci_lib.github_step_summary('ignored')


def test_step_summary_appends(monkeypatch, tmp_path):
	path = tmp_path / 'summary.md'
	monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(path))
	ci_lib.github_step_summary('first')
	ci_lib.github_step_summary('second')
	assert path.read_text() == 'first\n\nsecond\n\n'
