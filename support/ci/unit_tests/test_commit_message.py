import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import ci_lib
import pytest
from tools import make_release_notes as notes

CHECK_MESSAGE = ci_lib.ROOT_DIR / 'support' / 'scripts' / 'check-commit-message.py'

AUTHOR = ('Landing Author', 'landing@example.com')


def _env(author) -> dict:
	name, email = author
	return os.environ | {
		'GIT_AUTHOR_NAME': name,
		'GIT_AUTHOR_EMAIL': email,
		'GIT_COMMITTER_NAME': name,
		'GIT_COMMITTER_EMAIL': email,
		'GIT_CONFIG_GLOBAL': '/dev/null',
		'GIT_CONFIG_SYSTEM': '/dev/null',
	}


def _git(repo: Path, *args: str, input: str | None = None, author=AUTHOR) -> str:
	return subprocess.run(
		['git', '-C', str(repo), *args],
		input=input,
		capture_output=True,
		text=True,
		check=True,
		env=_env(author),
	).stdout


def _add(repo: Path, message: str, *, author=AUTHOR) -> None:
	repo.joinpath(f'{len(list(repo.glob("*.txt")))}.txt').write_text(message)
	_git(repo, 'add', '.', author=author)
	_git(repo, 'commit', '--cleanup=verbatim', '-F', '-', input=message, author=author)


@pytest.fixture
def repo(tmp_path):
	path = tmp_path / 'repo'
	path.mkdir()
	_git(path, 'init', '-q', '-b', 'main')
	return path


def test_release_notes_promote_bullets_only(repo):
	_add(repo, 'chore: base\n')
	_add(
		repo,
		dedent("""\
			chore(ci): umbrella (#7)

			* feat(a): one

			fix: prose that merely reads like a subject
		"""),
	)

	entries = notes.parse_git_log('HEAD~1..HEAD', dir=repo)

	assert [entry.message for entry in entries] == ['feat(a): one']


def test_a_malformed_bullet_is_an_error_not_body():
	message = dedent("""\
		chore(ci): umbrella (#7)

		* feat(a): one
		* just a note
	""")

	result = subprocess.run(
		[sys.executable, str(CHECK_MESSAGE), '--message-text', message],
		capture_output=True,
		text=True,
	)

	assert result.returncode == 1
	assert 'just a note' in result.stdout


def test_a_control_character_is_rejected():
	result = subprocess.run(
		[
			sys.executable,
			str(CHECK_MESSAGE),
			'--message-text',
			'fix(a): before\x01after\n\nA body long enough to pass the length rule.\n',
		],
		capture_output=True,
		text=True,
	)

	assert result.returncode == 1
	assert 'Control character' in result.stdout


def test_an_emoji_sequence_is_not_a_control_character():
	result = subprocess.run(
		[
			sys.executable,
			str(CHECK_MESSAGE),
			'--message-text',
			'feat(a): one 👨‍💻\n',
		],
		capture_output=True,
		text=True,
	)

	assert result.returncode == 0, result.stdout
