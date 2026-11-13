import json
from types import SimpleNamespace

import gh_common
import pytest
from tools import full_tests_command as command
from tools import pr_action_panel as panel


def comment_deleted(*_args):
	raise RuntimeError('comment deleted')


@pytest.fixture(autouse=True)
def context():
	gh_common.set_ctx(
		gh_common.Ctx(
			manager_repo='org/manager',
			executor_repo='org/executor',
			_pr_number='42',
			_head_ref=None,
		)
	)


def test_start_dispatches_without_changing_sticky_label(monkeypatch):
	monkeypatch.setenv('COMMENT_BODY', '/genvm-run-tests')
	monkeypatch.setenv('COMMENT_ID', '9001')
	monkeypatch.setenv('SENDER', 'maintainer')
	monkeypatch.setenv('REQUEST_ID', '700-1')
	monkeypatch.setattr(gh_common, 'has_write_access', lambda _sender: True)
	monkeypatch.setattr(
		command,
		'labels',
		lambda: {'ci-safe', 'run-full-tests', 'test-release-pipeline'},
	)
	monkeypatch.setattr(
		command,
		'pull_request',
		lambda: {
			'state': 'open',
			'head': {
				'ref': 'feat/change',
				'sha': 'a' * 40,
				'repo': {'full_name': 'org/manager'},
			},
		},
	)
	monkeypatch.setattr(command, 'add_reaction', lambda *_args: '77')

	dispatched = []
	monkeypatch.setattr(
		command,
		'dispatch',
		lambda head, **kwargs: dispatched.append((head, kwargs)),
	)
	run = {
		'databaseId': 123,
		'headSha': 'b' * 40,
		'url': 'https://example.test/run/123',
	}
	monkeypatch.setattr(command, 'wait_for_run', lambda *_args: run)
	statuses = []
	monkeypatch.setattr(
		command,
		'post_running_status',
		lambda *args: statuses.append(args),
	)

	assert command.start() == 0
	assert dispatched == [
		(
			'feat/change',
			{
				'expected_sha': 'a' * 40,
				'release_pipeline_test': True,
				'request': '9001',
				'request_id': '700-1',
			},
		)
	]
	assert statuses == [(run, '9001', '77', 'b' * 40)]


def test_start_rejects_untrusted_actor(monkeypatch):
	monkeypatch.setenv('COMMENT_BODY', '/genvm-run-tests')
	monkeypatch.setenv('SENDER', 'stranger')
	monkeypatch.setattr(gh_common, 'has_write_access', lambda _sender: False)

	with pytest.raises(SystemExit, match='no write access'):
		command.start()


def test_start_reporting_failure_does_not_fail_dispatched_tests(monkeypatch):
	monkeypatch.setenv('COMMENT_BODY', '/genvm-run-tests')
	monkeypatch.setenv('COMMENT_ID', '9001')
	monkeypatch.setenv('SENDER', 'maintainer')
	monkeypatch.setenv('REQUEST_ID', '700-1')
	monkeypatch.setattr(gh_common, 'has_write_access', lambda _sender: True)
	monkeypatch.setattr(command, 'labels', lambda: {'ci-safe'})
	monkeypatch.setattr(
		command,
		'pull_request',
		lambda: {
			'state': 'open',
			'head': {
				'ref': 'feat/change',
				'sha': 'a' * 40,
				'repo': {'full_name': 'org/manager'},
			},
		},
	)
	monkeypatch.setattr(command, 'add_reaction', lambda *_args: '77')
	dispatched = []
	monkeypatch.setattr(
		command,
		'dispatch',
		lambda *_args, **_kwargs: dispatched.append(True),
	)
	monkeypatch.setattr(
		command,
		'wait_for_run',
		comment_deleted,
	)
	monkeypatch.setattr(command, 'delete_reaction', lambda *_args: None)

	assert command.start() == 0
	assert dispatched == [True]


def test_delete_reaction_uses_issue_comment_endpoint(monkeypatch):
	calls = []
	monkeypatch.setattr(
		command,
		'gh',
		lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(stdout=''),
	)

	command.delete_reaction('9001', '77')

	assert 'repos/org/manager/issues/comments/9001/reactions/77' in calls[0][0]


def test_force_panel_still_sets_label_and_dispatches(monkeypatch):
	monkeypatch.setenv('COMMENT_ID', '8001')
	monkeypatch.setenv('SENDER', 'maintainer')
	monkeypatch.setattr(panel, 'labels', lambda: {'ci-safe'})
	monkeypatch.setattr(gh_common, 'has_write_access', lambda _sender: True)
	writes = []

	def gh(*args, **_kwargs):
		if args[:2] == ('api', 'repos/org/manager/issues/comments/8001'):
			return """{
				"body": "<!-- genvm-actions -->\\n- [x] Force run full tests <!-- action:force -->",
				"author": "github-actions[bot]"
			}"""
		writes.append(args)
		return ''

	monkeypatch.setattr(panel, 'gh', gh)
	dispatches = []
	monkeypatch.setattr(
		panel,
		'dispatch_full_tests',
		lambda **kwargs: dispatches.append(kwargs),
	)

	panel.run_panel()

	assert any('labels[]=run-full-tests' in call for call in writes)
	assert dispatches == [{'release_pipeline_test': False}]


@pytest.mark.parametrize(
	('conclusion', 'reaction', 'message'),
	[
		('success', 'rocket', '🚀 Full tests passed'),
		('failure', 'confused', '⚠️ Full tests failure'),
		('cancelled', 'confused', '⚠️ Full tests cancelled'),
	],
)
def test_complete_updates_status_and_reaction(
	monkeypatch, conclusion, reaction, message
):
	monkeypatch.setenv('RUN_ID', '123')
	monkeypatch.setenv('RUN_NAME', 'GenVM full [request=700-1;comment=9001;pr=42]')
	monkeypatch.setenv('RUN_URL', 'https://example.test/run/123')
	monkeypatch.setenv('RUN_SHA', 'a' * 40)
	monkeypatch.setenv('RUN_CONCLUSION', conclusion)
	monkeypatch.setattr(
		command,
		'status_comment',
		lambda _issue, _run, _request: {
			'id': 55,
			'body': '<!-- genvm-full-tests run=123 request=9001 eyes=77 -->\nold',
		},
	)
	deleted = []
	added = []
	monkeypatch.setattr(
		command,
		'delete_reaction',
		lambda comment, value: deleted.append((comment, value)),
	)
	monkeypatch.setattr(
		command, 'add_reaction', lambda comment, value: added.append((comment, value))
	)
	calls = []
	monkeypatch.setattr(
		command,
		'gh',
		lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(stdout=''),
	)

	assert command.complete() == 0
	assert deleted == [('9001', '77')]
	assert added == [('9001', reaction)]
	assert message in calls[-1][0][-1]
	assert 'https://example.test/run/123' in calls[-1][0][-1]


def test_complete_deleted_comment_does_not_fail_ci(monkeypatch):
	monkeypatch.setenv('RUN_ID', '123')
	monkeypatch.setenv('RUN_NAME', 'GenVM full [request=700-1;comment=9001;pr=42]')
	monkeypatch.setenv('RUN_CONCLUSION', 'failure')
	monkeypatch.setattr(
		command,
		'wait_for_status_comment',
		comment_deleted,
	)
	added = []
	monkeypatch.setattr(command, 'add_reaction', lambda *args: added.append(args))

	assert command.complete() == 0
	assert added == []


def test_find_run_matches_unique_request(monkeypatch):
	monkeypatch.setattr(
		command,
		'gh',
		lambda *_args, **_kwargs: SimpleNamespace(
			stdout="""[
				{"databaseId": 1, "displayTitle": "other", "headSha": "aaa", "url": "x"},
				{"databaseId": 2, "displayTitle": "full [request=700-1;comment=9;pr=42]", "headSha": "bbb", "url": "y"}
			]"""
		),
	)

	assert command.find_run('700-1')['databaseId'] == 2
	assert command.find_run('701-1') is None


def test_status_comment_ignores_forged_marker(monkeypatch):
	comments = [
		{
			'id': 1,
			'body': '<!-- genvm-full-tests run=123 request=9 eyes=7 -->',
			'user': {'login': 'attacker'},
		},
		{
			'id': 2,
			'body': '<!-- genvm-full-tests run=123 request=8 eyes=7 -->',
			'user': {'login': 'github-actions[bot]'},
		},
		{
			'id': 3,
			'body': '<!-- genvm-full-tests run=123 request=9 eyes=7 -->',
			'user': {'login': 'github-actions[bot]'},
		},
	]
	monkeypatch.setattr(
		command,
		'gh',
		lambda *_args, **_kwargs: SimpleNamespace(
			stdout='\n'.join(json.dumps(comment) for comment in comments)
		),
	)

	assert command.status_comment('42', '123', '9')['id'] == 3
