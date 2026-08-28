#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import time

import ci_lib
import gh_common
from gh_common import pr_number, repo

COMMAND = '/genvm-run-tests'
CI_SAFE_LABEL = 'ci-safe'
RELEASE_TEST_LABEL = 'test-release-pipeline'
REQUEST_RE = re.compile(r'\[request=(\d+-\d+);comment=(\d+);pr=(\d+)\]')
STATUS_RE = re.compile(r'<!-- genvm-full-tests run=(\d+) request=(\d+) eyes=(\d+) -->')
POLL_ATTEMPTS = 30
POLL_DELAY_SECONDS = 1


def gh(
	*args: str,
	check: bool = True,
	retry: bool = True,
) -> subprocess.CompletedProcess:
	return gh_common.gh(
		*args,
		token=gh_common.manager_token(),
		check=check,
		retry=retry,
	)


def labels() -> set[str]:
	result = gh(
		'api',
		'--paginate',
		f'repos/{repo()}/issues/{pr_number()}/labels?per_page=100',
		'--jq',
		'.[].name',
	)
	return set(result.stdout.splitlines())


def add_reaction(comment: str, content: str) -> str:
	result = gh(
		'api',
		'--method',
		'POST',
		f'repos/{repo()}/issues/comments/{comment}/reactions',
		'-f',
		f'content={content}',
		retry=False,
	)
	return str(json.loads(result.stdout)['id'])


def best_effort_add_reaction(comment: str, content: str) -> str | None:
	try:
		return add_reaction(comment, content)
	except Exception as error:
		print(f'::warning::could not add {content} reaction: {error}')
		return None


def delete_reaction(comment: str, reaction: str) -> None:
	gh(
		'api',
		'--method',
		'DELETE',
		f'repos/{repo()}/issues/comments/{comment}/reactions/{reaction}',
		check=False,
		retry=False,
	)


def pull_request() -> dict:
	return json.loads(gh('api', f'repos/{repo()}/pulls/{pr_number()}').stdout)


def dispatch(
	head: str,
	*,
	expected_sha: str,
	release_pipeline_test: bool,
	request: str,
	request_id: str,
) -> None:
	gh(
		'workflow',
		'run',
		'queue.yaml',
		'--repo',
		repo(),
		'--ref',
		head,
		'-f',
		f'pr={pr_number()}',
		'-f',
		f'release_pipeline_test={str(release_pipeline_test).lower()}',
		'-f',
		f'request_comment={request}',
		'-f',
		f'request_id={request_id}',
		'-f',
		f'expected_sha={expected_sha}',
		retry=False,
	)


def find_run(request_id: str) -> dict | None:
	result = gh(
		'run',
		'list',
		'--repo',
		repo(),
		'--workflow',
		'queue.yaml',
		'--event',
		'workflow_dispatch',
		'--limit',
		'50',
		'--json',
		'databaseId,displayTitle,headSha,url',
	)
	marker = f'[request={request_id};'
	for run in json.loads(result.stdout):
		if marker in run['displayTitle']:
			return run
	return None


def wait_for_run(request_id: str) -> dict:
	for attempt in range(POLL_ATTEMPTS):
		if run := find_run(request_id):
			return run
		if attempt + 1 < POLL_ATTEMPTS:
			time.sleep(POLL_DELAY_SECONDS)
	raise RuntimeError('dispatched full tests but could not find the workflow run')


def post_running_status(run: dict, request: str, reaction: str, sha: str) -> None:
	body = (
		f'<!-- genvm-full-tests run={run["databaseId"]} request={request} '
		f'eyes={reaction} -->\n'
		f'👀 Full tests are running for `{sha[:12]}`: '
		f'[open run #{run["databaseId"]}]({run["url"]})'
	)
	gh(
		'api',
		'--method',
		'POST',
		f'repos/{repo()}/issues/{pr_number()}/comments',
		'-f',
		f'body={body}',
		retry=False,
	)


def start() -> int:
	if os.environ.get('COMMENT_BODY', '').strip() != COMMAND:
		print('comment is not an exact /genvm-run-tests command; ignoring')
		return 0
	if not gh_common.has_write_access(os.environ['SENDER']):
		raise SystemExit(f'`{os.environ["SENDER"]}` has no write access to {repo()}')
	current_labels = labels()
	if CI_SAFE_LABEL not in current_labels:
		raise SystemExit(f'PR #{pr_number()} lacks the `{CI_SAFE_LABEL}` label')

	pr = pull_request()
	if pr['state'] != 'open':
		raise SystemExit(f'PR #{pr_number()} is not open')
	head_repo = pr['head'].get('repo')
	if head_repo is None or head_repo['full_name'] != repo():
		raise SystemExit('full tests cannot be dispatched for a fork PR')

	request = os.environ['COMMENT_ID']
	request_id = os.environ['REQUEST_ID']
	reaction = best_effort_add_reaction(request, 'eyes') or '0'
	try:
		dispatch(
			pr['head']['ref'],
			expected_sha=pr['head']['sha'],
			release_pipeline_test=RELEASE_TEST_LABEL in current_labels,
			request=request,
			request_id=request_id,
		)
	except Exception:
		if reaction != '0':
			delete_reaction(request, reaction)
		best_effort_add_reaction(request, 'confused')
		raise
	try:
		run = wait_for_run(request_id)
		post_running_status(run, request, reaction, run['headSha'])
	except Exception as error:
		print(f'::warning::full tests were dispatched but status reporting failed: {error}')
		if reaction != '0':
			delete_reaction(request, reaction)
		best_effort_add_reaction(request, 'confused')
	return 0


def request_from_run_name(name: str) -> tuple[str, str, str] | None:
	match = REQUEST_RE.search(name)
	return (match.group(1), match.group(2), match.group(3)) if match else None


def status_comment(issue: str, run: str, request: str) -> dict | None:
	result = gh(
		'api',
		'--paginate',
		f'repos/{repo()}/issues/{issue}/comments?per_page=100',
		'--jq',
		'.[] | @json',
	)
	for line in result.stdout.splitlines():
		comment = json.loads(line)
		match = STATUS_RE.search(comment['body'])
		if (
			match
			and match.group(1) == run
			and match.group(2) == request
			and comment.get('user', {}).get('login') == 'github-actions[bot]'
		):
			return comment
	return None


def wait_for_status_comment(issue: str, run: str, request: str) -> dict:
	for attempt in range(POLL_ATTEMPTS):
		if comment := status_comment(issue, run, request):
			return comment
		if attempt + 1 < POLL_ATTEMPTS:
			time.sleep(POLL_DELAY_SECONDS)
	raise RuntimeError(f'could not find the status comment for full-test run {run}')


def complete() -> int:
	request_info = request_from_run_name(os.environ.get('RUN_NAME', ''))
	if request_info is None:
		print('run was not started by /genvm-run-tests; nothing to report')
		return 0
	_request_id, title_request, issue = request_info
	run = os.environ['RUN_ID']
	conclusion = os.environ.get('RUN_CONCLUSION', '') or 'incomplete'
	passed = conclusion == 'success'
	reaction = 'rocket' if passed else 'confused'
	try:
		comment = wait_for_status_comment(issue, run, title_request)
		match = STATUS_RE.search(comment['body'])
		assert match is not None
		request = match.group(2)
		status = (
			'🚀 Full tests passed'
			if passed
			else f'⚠️ Full tests {conclusion.replace("_", " ")}'
		)
		body = (
			f'{match.group(0)}\n{status} for `{os.environ["RUN_SHA"][:12]}`: '
			f'[open run #{run}]({os.environ["RUN_URL"]})'
		)
	except Exception as error:
		print(f'::warning::could not authenticate full-test status comment: {error}')
		return 0
	try:
		gh(
			'api',
			'--method',
			'PATCH',
			f'repos/{repo()}/issues/comments/{comment["id"]}',
			'-f',
			f'body={body}',
			retry=False,
		)
	except Exception as error:
		print(f'::warning::could not update full-test status comment: {error}')
	if match.group(3) != '0':
		delete_reaction(request, match.group(3))
	best_effort_add_reaction(request, reaction)
	return 0


class FullTestsCommand(ci_lib.Tool):
	"""Start or report a `/genvm-run-tests` request."""

	def name(self) -> str:
		return 'full-tests-command'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		gh_common.add_args(parser, executor_repo=False, head_ref=False)
		parser.add_argument('operation', choices=('start', 'complete'))

	def handler(self, args: argparse.Namespace) -> int:
		gh_common.set_ctx(gh_common.Ctx.from_args(args))
		return start() if args.operation == 'start' else complete()


COMMANDS = [FullTestsCommand()]
