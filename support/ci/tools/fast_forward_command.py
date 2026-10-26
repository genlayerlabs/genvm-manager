#!/usr/bin/env python3
"""
`/genvm-fast-forward`: move a PR's base branch to the PR's exact head.

The handler runs from the default branch with a write deploy key in scope, so it
reads the PR only through the API and never executes its code. The deploy key
bypasses the base branch's rulesets, which is why every gate lives here:

1. The commenter is an admin, or a writer and the head carries an approval from
	a writer other than the author and the commenter, with no writer's change
	request outstanding
2. The base is a dev branch, or a release branch and the commenter is an admin;
	the PR is not from a fork
3. `E2E Tests` and every check the base's rulesets require concluded `success`
	on the head, and the rulesets hold nothing else the push could violate
4. The head is strictly ahead of the base tip, without merge commits

The push is a plain non-force push of the verified SHA, so the server enforces
the fast-forward again. Executor branches follow through the usual sync of a
manager branch push
"""

import argparse
import json
import os
import re
import subprocess
import urllib.parse

import ci_lib
import gh_common
from gh_common import pr_number, repo
from tools.full_tests_command import best_effort_add_reaction, delete_reaction

COMMAND = '/genvm-fast-forward'
E2E_CHECK = 'E2E Tests'
E2E_APP = 2929262
DEV_BRANCH_RE = re.compile(r'^v\d+\.\d+-dev$')
RELEASE_BRANCH_RE = re.compile(r'^v\d+\.\d+$')
# what the push itself must honor; everything else is the bypass's business
UNDERSTOOD_RULES = frozenset(
	{
		'creation',
		'deletion',
		'non_fast_forward',
		'required_linear_history',
		'required_status_checks',
		'update',
	}
)


class Refused(RuntimeError):
	pass


def gh_json(path: str) -> dict:
	return json.loads(gh_common.gh_manager('api', path))


def gh_items(path: str, jq: str = '.[]') -> list[dict]:
	out = gh_common.gh_manager('api', '--paginate', path, '--jq', f'{jq} | @json')
	return [json.loads(line) for line in out.splitlines()]


def permission(login: str) -> str | None:
	result = gh_common.gh(
		'api',
		f'repos/{repo()}/collaborators/{urllib.parse.quote(login)}/permission',
		token=gh_common.manager_token(),
		check=False,
	)
	if result.returncode == 0:
		return json.loads(result.stdout).get('permission')
	# anything but "not a collaborator" must not quietly drop a change request
	if 'HTTP 403' in result.stderr or 'HTTP 404' in result.stderr:
		return None
	raise RuntimeError(f'could not read the permission of `{login}`')


def base_rules(base: str) -> list[dict]:
	return gh_items(f'repos/{repo()}/rules/branches/{urllib.parse.quote(base, safe="")}')


def rule_failures(rules: list[dict]) -> list[str]:
	return [
		f'the base has a `{rule["type"]}` rule this command does not enforce'
		for rule in rules
		if rule['type'] not in UNDERSTOOD_RULES
	]


def required_checks(rules: list[dict]) -> list[tuple[str, int | None]]:
	required = [
		(check['context'], check.get('integration_id'))
		for rule in rules
		if rule['type'] == 'required_status_checks'
		for check in rule['parameters']['required_status_checks']
	]
	if all(context != E2E_CHECK for context, _app in required):
		required.append((E2E_CHECK, E2E_APP))
	return required


def check_failures(
	required: list[tuple[str, int | None]], runs: list[dict], statuses: list[dict]
) -> list[str]:
	failures = []
	for context, app in required:
		matching = [
			run
			for run in runs
			if run['name'] == context and (app is None or run['app']['id'] == app)
		]
		if matching:
			# a rerun is a new check run, and GitHub judges the latest one
			run = max(matching, key=lambda run: run['id'])
			if run['status'] != 'completed':
				failures.append(f'`{context}` is {run["status"].replace("_", " ")}')
			elif run['conclusion'] != 'success':
				failures.append(f'`{context}` concluded `{run["conclusion"]}`')
			continue
		# statuses come newest first and carry no app to match against
		status = next((s for s in statuses if s['context'] == context), None)
		if app is not None or status is None:
			failures.append(f'`{context}` has not run')
		elif status['state'] != 'success':
			failures.append(f'`{context}` is `{status["state"]}`')
	return failures


def review_failures(
	*,
	sender: str,
	sender_permission: str | None,
	author: str,
	head_sha: str,
	reviews: list[dict],
	permission_of,
) -> list[str]:
	if sender_permission == 'admin':
		return []
	if sender_permission not in gh_common.WRITE_ROLES:
		return [f'`{sender}` is neither an admin nor a writer']

	# reviews come oldest first; a plain comment neither grants nor revokes
	latest: dict[str, dict] = {}
	for review in reviews:
		login = (review.get('user') or {}).get('login')
		if login and review['state'] in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
			latest[login] = review

	failures = []
	approved = False
	for login, review in latest.items():
		if (
			review['user'].get('type') != 'User'
			or permission_of(login) not in gh_common.WRITE_ROLES
		):
			continue
		if review['state'] == 'CHANGES_REQUESTED':
			failures.append(f'`{login}` requested changes')
		elif (
			review['state'] == 'APPROVED'
			and review['commit_id'] == head_sha
			and login not in (author, sender)
		):
			approved = True
	if not approved:
		failures.append(
			f'no writer other than the author and `{sender}` approved `{head_sha[:12]}`'
		)
	return failures


def gate(pr: dict, sender: str) -> list[str]:
	if pr['state'] != 'open':
		return ['the PR is not open']
	base = pr['base']['ref']
	head_sha = pr['head']['sha']
	failures = []
	if pr['draft']:
		failures.append('the PR is a draft')
	# a fork skips the executor precondition, so its gitlinks are unproven
	head_repo = pr['head'].get('repo')
	if head_repo is None or head_repo['full_name'] != repo():
		failures.append('the PR comes from a fork')
	sender_permission = permission(sender)
	if RELEASE_BRANCH_RE.fullmatch(base):
		if sender_permission != 'admin':
			failures.append(f'only an admin may fast-forward the release branch `{base}`')
	elif not DEV_BRANCH_RE.fullmatch(base):
		failures.append(f'`{base}` is neither a dev nor a release branch')

	rules = base_rules(base)
	failures += rule_failures(rules)
	failures += review_failures(
		sender=sender,
		sender_permission=sender_permission,
		author=pr['user']['login'],
		head_sha=head_sha,
		reviews=gh_items(f'repos/{repo()}/pulls/{pr_number()}/reviews?per_page=100'),
		permission_of=permission,
	)
	failures += check_failures(
		required_checks(rules),
		gh_items(
			f'repos/{repo()}/commits/{head_sha}/check-runs?per_page=100', '.check_runs[]'
		),
		gh_items(f'repos/{repo()}/commits/{head_sha}/statuses?per_page=100'),
	)

	compared = gh_json(
		f'repos/{repo()}/compare/{urllib.parse.quote(base, safe="")}...{head_sha}'
	)['status']
	if compared == 'identical':
		failures.append(f'`{base}` is already at `{head_sha[:12]}`')
	elif compared != 'ahead':
		failures.append(f'`{head_sha[:12]}` is {compared} relative to `{base}`; rebase it')
	return failures


def git(*args: str) -> subprocess.CompletedProcess:
	return ci_lib.run(['git', *args], check=False, capture_output=True)


def fast_forward(base: str, head_sha: str) -> None:
	fetched = git(
		'fetch',
		'--no-tags',
		'origin',
		f'+refs/heads/{base}:refs/genvm-ff/base',
		f'+refs/pull/{pr_number()}/head:refs/genvm-ff/head',
	)
	if fetched.returncode != 0:
		raise RuntimeError(
			f'could not fetch `{base}` and the PR head: {fetched.stderr.strip()}'
		)
	# a head pushed during the checks must not land unverified
	if git('rev-parse', 'refs/genvm-ff/head').stdout.strip() != head_sha:
		raise Refused('the head moved during verification; comment again')
	merges = git('rev-list', '--min-parents=2', 'refs/genvm-ff/base..refs/genvm-ff/head')
	if merges.returncode != 0 or merges.stdout.strip():
		raise Refused(f'`{base}` requires linear history, and the PR has merge commits')
	pushed = git('push', 'origin', f'{head_sha}:refs/heads/{base}')
	if pushed.returncode != 0:
		raise Refused(f'`{base}` rejected the push:\n```\n{pushed.stderr.strip()}\n```')


def comment(body: str) -> None:
	gh_common.gh_manager(
		'api',
		'--method',
		'POST',
		f'repos/{repo()}/issues/{pr_number()}/comments',
		'-f',
		f'body={body}',
		retry=False,
	)


def run() -> int:
	if os.environ.get('COMMENT_BODY', '').strip() != COMMAND:
		print('comment is not an exact /genvm-fast-forward command; ignoring')
		return 0
	request = os.environ['COMMENT_ID']
	sender = os.environ['SENDER']
	run_url = os.environ['RUN_URL']
	eyes = best_effort_add_reaction(request, 'eyes')

	def finish(reaction: str, body: str) -> None:
		if eyes is not None:
			delete_reaction(request, eyes)
		best_effort_add_reaction(request, reaction)
		comment(f'{body}\n\n[Open run]({run_url})')

	try:
		pr = gh_json(f'repos/{repo()}/pulls/{pr_number()}')
		base = pr['base']['ref']
		head_sha = pr['head']['sha']
		if failures := gate(pr, sender):
			raise Refused('\n'.join(f'- {failure}' for failure in failures))
		fast_forward(base, head_sha)
	except Refused as refusal:
		finish('confused', f'😕 Not fast-forwarding:\n{refusal}')
		return 1
	except Exception:
		finish('confused', '😕 `/genvm-fast-forward` failed unexpectedly')
		raise
	finish('rocket', f'🚀 Fast-forwarded `{base}` to `{head_sha[:12]}`')
	return 0


class FastForwardCommand(ci_lib.Tool):
	"""Fast-forward a PR's base branch to its head on `/genvm-fast-forward`."""

	def name(self) -> str:
		return 'fast-forward-command'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		gh_common.add_args(parser, executor_repo=False, head_ref=False)

	def handler(self, args: argparse.Namespace) -> int:
		gh_common.set_ctx(gh_common.Ctx.from_args(args))
		return run()


COMMANDS = [FastForwardCommand()]
