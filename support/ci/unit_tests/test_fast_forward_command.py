import subprocess

import gh_common
import pytest
from tools import fast_forward_command as command

HEAD = 'a' * 40
OLD = 'b' * 40
E2E_APP = 2929262
ACTIONS_APP = 15368
REQUIRED = [('validate-end', ACTIONS_APP), ('E2E Tests', E2E_APP)]


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


def check_run(name, app, conclusion='success', *, id=1, status='completed'):
	return {
		'id': id,
		'name': name,
		'app': {'id': app},
		'status': status,
		'conclusion': conclusion if status == 'completed' else None,
	}


def green_runs():
	return [check_run('validate-end', ACTIONS_APP), check_run('E2E Tests', E2E_APP)]


def review(login, state, commit=HEAD, user_type='User'):
	return {
		'user': {'login': login, 'type': user_type},
		'state': state,
		'commit_id': commit,
	}


PERMISSIONS = {
	'admin': 'admin',
	'writer': 'write',
	'other-writer': 'write',
	'reader': 'read',
	'coderabbitai[bot]': 'none',
}


def reviews_gate(reviews, sender='writer', author='author'):
	return command.review_failures(
		sender=sender,
		sender_permission=PERMISSIONS.get(sender),
		author=author,
		head_sha=HEAD,
		reviews=reviews,
		permission_of=PERMISSIONS.get,
	)


# -- Checks --


def test_green_required_checks_pass():
	assert command.check_failures(REQUIRED, green_runs(), []) == []


def test_missing_e2e_fails():
	runs = [check_run('validate-end', ACTIONS_APP)]
	assert command.check_failures(REQUIRED, runs, []) == ['`E2E Tests` has not run']


def test_pending_check_fails():
	runs = [
		check_run('validate-end', ACTIONS_APP),
		check_run('E2E Tests', E2E_APP, status='in_progress'),
	]
	assert command.check_failures(REQUIRED, runs, []) == ['`E2E Tests` is in progress']


def test_skipped_check_fails():
	runs = [
		check_run('validate-end', ACTIONS_APP, 'skipped'),
		check_run('E2E Tests', E2E_APP),
	]
	assert command.check_failures(REQUIRED, runs, []) == [
		'`validate-end` concluded `skipped`'
	]


def test_latest_rerun_decides():
	runs = [
		check_run('validate-end', ACTIONS_APP, 'failure', id=1),
		check_run('validate-end', ACTIONS_APP, 'success', id=2),
		check_run('E2E Tests', E2E_APP),
	]
	assert command.check_failures(REQUIRED, runs, []) == []
	runs[0]['id'] = 3
	assert command.check_failures(REQUIRED, runs, []) == [
		'`validate-end` concluded `failure`'
	]


def test_same_name_from_another_app_does_not_count():
	runs = [check_run('validate-end', ACTIONS_APP), check_run('E2E Tests', ACTIONS_APP)]
	assert command.check_failures(REQUIRED, runs, []) == ['`E2E Tests` has not run']


def test_status_satisfies_a_check_without_app():
	required = [('CodeRabbit', None)]
	statuses = [
		{'context': 'CodeRabbit', 'state': 'success'},
		{'context': 'CodeRabbit', 'state': 'pending'},
	]
	assert command.check_failures(required, [], statuses) == []
	statuses.reverse()
	assert command.check_failures(required, [], statuses) == ['`CodeRabbit` is `pending`']


def test_status_does_not_satisfy_a_check_bound_to_an_app():
	statuses = [{'context': 'E2E Tests', 'state': 'success'}]
	runs = [check_run('validate-end', ACTIONS_APP)]
	assert command.check_failures(REQUIRED, runs, statuses) == ['`E2E Tests` has not run']


RULES = [
	{
		'type': 'required_status_checks',
		'parameters': {
			'required_status_checks': [
				{'context': 'validate-end', 'integration_id': ACTIONS_APP}
			]
		},
	},
	{'type': 'non_fast_forward', 'parameters': None},
]


def test_e2e_is_required_from_its_app_even_when_rules_omit_it():
	assert command.required_checks(RULES) == [
		('validate-end', ACTIONS_APP),
		('E2E Tests', E2E_APP),
	]


def test_unknown_rule_fails_closed():
	rules = [*RULES, {'type': 'pull_request', 'parameters': {}}]
	assert command.rule_failures(RULES) == []
	assert command.rule_failures(rules) == [
		'the base has a `pull_request` rule this command does not enforce'
	]


# -- Reviews --


def test_admin_needs_no_approval():
	assert reviews_gate([], sender='admin') == []


def test_reader_is_refused():
	assert reviews_gate([review('other-writer', 'APPROVED')], sender='reader') == [
		'`reader` is neither an admin nor a writer'
	]


def test_writer_with_writer_approval_passes():
	assert reviews_gate([review('other-writer', 'APPROVED')]) == []


def test_writer_without_approval_fails():
	assert reviews_gate([]) == [
		f'no writer other than the author and `writer` approved `{HEAD[:12]}`'
	]


def test_stale_approval_does_not_count():
	assert reviews_gate([review('other-writer', 'APPROVED', commit=OLD)]) != []


def test_bot_and_reader_approvals_do_not_count():
	reviews = [
		review('coderabbitai[bot]', 'APPROVED', user_type='Bot'),
		review('reader', 'APPROVED'),
	]
	assert reviews_gate(reviews) != []


def test_author_approval_does_not_count():
	assert reviews_gate([review('writer', 'APPROVED')], author='writer') != []


def test_commenter_approval_does_not_count():
	assert reviews_gate([review('writer', 'APPROVED')], sender='writer') != []


def test_later_comment_keeps_approval():
	assert (
		reviews_gate(
			[review('other-writer', 'APPROVED'), review('other-writer', 'COMMENTED')]
		)
		== []
	)


def test_dismissed_approval_does_not_count():
	assert (
		reviews_gate(
			[review('other-writer', 'APPROVED'), review('other-writer', 'DISMISSED')]
		)
		!= []
	)


def test_writer_change_request_blocks_despite_approval():
	reviews = [
		review('other-writer', 'APPROVED'),
		review('admin', 'CHANGES_REQUESTED', commit=OLD),
	]
	assert reviews_gate(reviews) == ['`admin` requested changes']


def test_reader_change_request_does_not_block():
	assert (
		reviews_gate(
			[review('other-writer', 'APPROVED'), review('reader', 'CHANGES_REQUESTED')]
		)
		== []
	)


def test_deleted_reviewer_is_ignored():
	reviews = [
		{'user': None, 'state': 'APPROVED', 'commit_id': HEAD},
		review('other-writer', 'APPROVED'),
	]
	assert reviews_gate(reviews) == []


# -- Command --


def pr(**overrides):
	return {
		'state': 'open',
		'draft': False,
		'user': {'login': 'author'},
		'base': {'ref': 'v0.6-dev', 'repo': {'default_branch': 'main'}},
		'head': {'sha': HEAD, 'repo': {'full_name': 'org/manager'}},
		**overrides,
	}


@pytest.fixture
def api(monkeypatch):
	state = {
		'compare': 'ahead',
		'pr': pr(),
		'rules': RULES,
		'pushed': [],
		'comments': [],
		'reactions': [],
	}

	def gh_json(path):
		if '/compare/' in path:
			return {'status': state['compare']}
		return state['pr']

	def gh_items(path, jq='.[]'):
		if path.endswith('/reviews?per_page=100'):
			return [review('other-writer', 'APPROVED')]
		if '/check-runs' in path:
			return green_runs()
		return []

	monkeypatch.setattr(command, 'gh_json', gh_json)
	monkeypatch.setattr(command, 'gh_items', gh_items)
	monkeypatch.setattr(command, 'base_rules', lambda _base: state['rules'])
	monkeypatch.setattr(command, 'permission', PERMISSIONS.get)
	monkeypatch.setattr(
		command, 'fast_forward', lambda *args: state['pushed'].append(args)
	)
	monkeypatch.setattr(command, 'comment', state['comments'].append)
	monkeypatch.setattr(
		command,
		'best_effort_add_reaction',
		lambda _comment, content: state['reactions'].append(content) or '7',
	)
	monkeypatch.setattr(
		command, 'delete_reaction', lambda *_args: state['reactions'].append('-')
	)
	monkeypatch.setenv('COMMENT_BODY', '/genvm-fast-forward\n')
	monkeypatch.setenv('COMMENT_ID', '9001')
	monkeypatch.setenv('SENDER', 'writer')
	monkeypatch.setenv('RUN_URL', 'https://example.test/run/1')
	return state


def test_green_pr_is_fast_forwarded(api):
	assert command.run() == 0
	assert api['pushed'] == [('v0.6-dev', HEAD)]
	assert api['reactions'] == ['eyes', '-', 'rocket']
	assert api['comments'][0].startswith(f'🚀 Fast-forwarded `v0.6-dev` to `{HEAD[:12]}`')


def test_other_comment_is_ignored(api, monkeypatch):
	monkeypatch.setenv('COMMENT_BODY', '/genvm-fast-forward please')
	assert command.run() == 0
	assert api['pushed'] == api['reactions'] == []


def test_refusal_lists_every_failure(api):
	api['pr'] = pr(
		draft=True,
		base={'ref': 'main', 'repo': {'default_branch': 'main'}},
		head={'sha': HEAD, 'repo': {'full_name': 'someone/manager'}},
	)
	api['compare'] = 'diverged'
	assert command.run() == 1
	assert api['pushed'] == []
	assert api['reactions'] == ['eyes', '-', 'confused']
	body = api['comments'][0]
	assert '- the PR is a draft' in body, body
	assert '- the PR comes from a fork' in body, body
	assert '- `main` is neither a dev nor a release branch' in body, body
	assert f'- `{HEAD[:12]}` is diverged relative to `main`; rebase it' in body, body


@pytest.mark.parametrize(('sender', 'refused'), [('writer', True), ('admin', False)])
def test_release_branch_needs_an_admin(api, monkeypatch, sender, refused):
	monkeypatch.setenv('SENDER', sender)
	api['pr'] = pr(base={'ref': 'v0.6', 'repo': {'default_branch': 'main'}})
	assert command.run() == int(refused)
	assert (api['pushed'] == []) == refused


def test_unreadable_permission_raises(monkeypatch):
	monkeypatch.setattr(
		gh_common,
		'gh',
		lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, '', 'HTTP 502'),
	)
	with pytest.raises(RuntimeError):
		command.permission('writer')
	monkeypatch.setattr(
		gh_common,
		'gh',
		lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, '', 'HTTP 404'),
	)
	assert command.permission('stranger') is None


def test_up_to_date_base_is_refused(api):
	api['compare'] = 'identical'
	assert command.run() == 1
	assert api['pushed'] == []


def test_closed_pr_is_refused(api):
	api['pr'] = pr(state='closed')
	assert command.run() == 1
	assert '- the PR is not open' in api['comments'][0]


def test_rejected_push_is_reported(api, monkeypatch):
	def reject(*_args):
		raise command.Refused('`v0.6-dev` rejected the push')

	monkeypatch.setattr(command, 'fast_forward', reject)
	assert command.run() == 1
	assert api['reactions'][-1] == 'confused'
	assert 'rejected the push' in api['comments'][0]


def test_unexpected_error_is_reported_and_raised(api, monkeypatch):
	def boom(_path):
		raise RuntimeError('api down')

	monkeypatch.setattr(command, 'gh_json', boom)
	with pytest.raises(RuntimeError):
		command.run()
	assert api['reactions'][-1] == 'confused'
	assert 'failed unexpectedly' in api['comments'][0]
