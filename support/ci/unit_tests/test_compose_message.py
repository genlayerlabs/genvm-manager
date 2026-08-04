import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import ci_lib
import gh_common
import pytest
from tools import genvm_merge_into_dev as merge
from tools import make_release_notes as notes

CHECK_MESSAGE = ci_lib.ROOT_DIR / 'support' / 'scripts' / 'check-commit-message.py'

AUTHOR = ('Landing Author', 'landing@example.com')
OTHER = ('Other Author', 'other@example.com')
PR = {'title': 'chore(ci): stitch the commits'}


def _env(author) -> dict:
	name, email = author
	return os.environ | {
		'GIT_AUTHOR_NAME': name,
		'GIT_AUTHOR_EMAIL': email,
		'GIT_COMMITTER_NAME': name,
		'GIT_COMMITTER_EMAIL': email,
		# The tests assert on exact message text, so no user config may reach git.
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
	"""
	One commit carrying `message` verbatim -- `--cleanup=verbatim` so git keeps
	the blank lines the format under test is made of.
	"""
	repo.joinpath(f'{len(list(repo.glob("*.txt")))}.txt').write_text(message)
	_git(repo, 'add', '.', author=author)
	_git(repo, 'commit', '--cleanup=verbatim', '-F', '-', input=message, author=author)


def _compose(since: str = 'HEAD~2') -> str:
	return merge.compose_message(PR, None, f'{since}..HEAD', AUTHOR, qualified=False)


@pytest.fixture
def repo(tmp_path, monkeypatch):
	path = tmp_path / 'repo'
	path.mkdir()
	_git(path, 'init', '-q', '-b', 'main')
	monkeypatch.chdir(path)
	monkeypatch.setattr(
		gh_common,
		'_CTX',
		gh_common.Ctx(
			manager_repo='genlayerlabs/genvm-manager',
			executor_repo='genlayerlabs/genvm-executor',
			_pr_number='7',
			_head_ref='feat/x',
		),
	)
	return path


def test_bodies_are_kept_after_their_bullet(repo):
	_add(repo, 'chore: base\n')
	_add(repo, 'feat(a): one\n')
	_add(repo, 'fix(b): two\n\nThe parser lied about EOF.\nSecond line.\n')

	assert _compose() == dedent("""\
		chore(ci): stitch the commits (#7)

		* feat(a): one
		* fix(b): two

		The parser lied about EOF.
		Second line.
	""")


def test_bullets_stay_adjacent_when_bodies_are_absent(repo):
	_add(repo, 'chore: base\n')
	_add(repo, 'feat(a): one\n')
	_add(repo, 'fix(b): two\n')

	assert _compose() == dedent("""\
		chore(ci): stitch the commits (#7)

		* feat(a): one
		* fix(b): two
	""")


def test_a_body_is_blank_line_delimited_on_both_sides(repo):
	_add(repo, 'chore: base\n')
	_add(repo, 'feat(a): one\n\nWhy one.\n')
	_add(repo, 'fix(b): two\n')

	assert _compose() == dedent("""\
		chore(ci): stitch the commits (#7)

		* feat(a): one

		Why one.

		* fix(b): two
	""")


def test_squashing_a_squash_flattens_its_bullets(repo):
	"""
	Squashing is associative: the inner umbrella subject stays as a redundant
	bullet, and everything under it keeps the level a first-hand squash gave it.
	"""
	_add(repo, 'chore: base\n')
	_add(repo, 'chore(ci): landed earlier (#3)\n\n* feat(a): b\n* feat(a): c\n')
	_add(repo, 'feat(d): d\n\n* feat(d): e\n* feat(d): f\n')

	assert _compose() == dedent("""\
		chore(ci): stitch the commits (#7)

		* chore(ci): landed earlier (#3)
		* feat(a): b
		* feat(a): c
		* feat(d): d
		* feat(d): e
		* feat(d): f
	""")


def test_prose_keeps_its_blank_lines_among_the_bullets(repo):
	_add(repo, 'chore: base\n')
	_add(repo, 'feat(a): one\n\n* feat(a): nested\nwhy nested.\n')
	_add(repo, 'fix(b): two\n')

	assert _compose() == dedent("""\
		chore(ci): stitch the commits (#7)

		* feat(a): one
		* feat(a): nested

		why nested.

		* fix(b): two
	""")


def test_a_body_keeps_its_own_paragraph_breaks(repo):
	_add(repo, 'chore: base\n')
	_add(repo, 'feat(a): one\n\nwhat it does.\n\nwhy it does it.\n')
	_add(repo, 'fix(b): two\n')

	assert _compose() == dedent("""\
		chore(ci): stitch the commits (#7)

		* feat(a): one

		what it does.

		why it does it.

		* fix(b): two
	""")


def test_a_lone_commit_titled_like_the_pr_keeps_its_body(repo):
	_add(repo, 'chore: base\n')
	_add(repo, f'{PR["title"]}\n\nThe only rationale there is.\n')

	assert _compose('HEAD~1') == dedent("""\
		chore(ci): stitch the commits (#7)

		The only rationale there is.
	""")


def test_coauthors_follow_the_last_body(repo):
	_add(repo, 'chore: base\n')
	_add(repo, 'feat(a): one\n\nWhy one.\n', author=OTHER)

	assert _compose('HEAD~1') == dedent("""\
		chore(ci): stitch the commits (#7)

		* feat(a): one

		Why one.

		Co-authored-by: Other Author <other@example.com>
	""")


def test_the_composed_message_passes_the_commit_hook(repo):
	"""
	Nothing re-validates a generated message, so the hook is checked here instead.
	"""
	_add(repo, 'chore: base\n')
	_add(repo, 'feat(a): one ✨\n\n* fix(a): nested ♻️\nWhy one, at length.\n')
	_add(repo, 'fix(b): two 🐛\n')

	result = subprocess.run(
		[sys.executable, str(CHECK_MESSAGE), '--message-text', _compose()],
		capture_output=True,
		text=True,
	)

	assert result.returncode == 0, result.stdout


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

	assert [e.message for e in entries] == ['feat(a): one']


def test_a_malformed_bullet_is_an_error_not_body(repo):
	"""
	The format rests on `*` meaning bullet, so a `*` line that is not a subject
	cannot be quietly kept as prose -- it would flatten into a broken entry.
	"""
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


def test_a_control_character_is_rejected(repo):
	"""
	`commit_entries` reads a range as lines, so a message may only be made of
	those; a control character elsewhere in it is invisible where it is written.
	"""
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


def test_an_emoji_sequence_is_not_a_control_character(repo):
	_add(repo, 'chore: base\n')
	_add(repo, 'feat(a): one 👨‍💻\n')

	result = subprocess.run(
		[sys.executable, str(CHECK_MESSAGE), '--message-text', _compose('HEAD~1')],
		capture_output=True,
		text=True,
	)

	assert result.returncode == 0, result.stdout
