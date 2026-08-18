from types import SimpleNamespace

import pytest
from tools import genvm_merge_into_dev as merge


class MergeBlocked(Exception):
	pass


def test_force_merge_still_requires_rebased(monkeypatch):
	head_sha = 'a' * 40
	pr = {
		'baseRefName': 'v0.6-dev',
		'headRefOid': head_sha,
		'isDraft': False,
		'state': 'OPEN',
		'title': 'fix(ci): keep the rebase gate',
	}
	monkeypatch.setenv('FORCE_ACTOR', 'repo-admin')
	monkeypatch.setattr(merge, 'pr_view', lambda *fields: pr)
	monkeypatch.setattr(merge, 'pr_number', lambda: '7')
	monkeypatch.setattr(merge, 'check_title', lambda value: None)
	monkeypatch.setattr(merge, 'announce_force', lambda actor, sha: None)

	def git(*args, **kwargs):
		if args[0] == 'fetch':
			return SimpleNamespace(stdout='')
		if args == ('rev-parse', 'refs/prhead'):
			return SimpleNamespace(stdout=f'{head_sha}\n')
		raise AssertionError(f'unexpected git call after rebase gate: {args}')

	def behind(base, head):
		assert base == 'origin/v0.6-dev'
		assert head == 'refs/prhead'
		return 2

	def block(message):
		raise MergeBlocked(message)

	monkeypatch.setattr(merge, 'git', git)
	monkeypatch.setattr(merge.behind_lib, 'behind_by_git', behind)
	monkeypatch.setattr(merge, 'block', block)

	with pytest.raises(MergeBlocked, match='2 commit.* behind'):
		merge.merge_into_dev()
