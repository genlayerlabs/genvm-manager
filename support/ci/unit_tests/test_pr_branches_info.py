import pytest

import pr_branches_info as info


@pytest.mark.parametrize('available', [False, True])
def test_new_executor_line_requires_remote_commit(monkeypatch, available):
	sha = 'a' * 40

	def submodule_sha(_repo, _path, ref, _token):
		return None if ref == 'manager-base' else sha

	monkeypatch.setattr(info, 'submodule_sha', submodule_sha)
	monkeypatch.setattr(info, 'commit_exists', lambda *_args: available)
	monkeypatch.setattr(info, 'existing_pr', lambda *_args: None)

	result = info.executor_info(
		'v0.4',
		'org/manager',
		'manager-base',
		'manager-head',
		'feature',
		'org/executor',
		'manager-token',
		'executor-token',
	)

	assert result.base_sha is None
	assert result.head_sha == sha
	assert result.synced is available
