import argparse
import json
import os

import ci_lib
import pr_branches_info
from pr_branches_info import RepoInfo


def force_push_head(repo: str, branch: str, sha: str, token: str) -> str:
	"""Point `refs/heads/<branch>` in `repo` at `sha`, creating or force-moving it.

	`sha` must already be an object in `repo` (the executor commit the manager
	gitlink pins). Returns 'created' or 'updated'.
	"""
	# Force-update an existing branch first (the common re-provision case); if the
	# branch does not exist yet, create it.
	updated = pr_branches_info.gh(
		'api',
		'--method',
		'PATCH',
		f'repos/{repo}/git/refs/heads/{branch}',
		'-f',
		f'sha={sha}',
		'-F',
		'force=true',
		token=token,
		check=False,
	)
	if updated.returncode == 0:
		return 'updated'
	created = pr_branches_info.gh(
		'api',
		'--method',
		'POST',
		f'repos/{repo}/git/refs',
		'-f',
		f'ref=refs/heads/{branch}',
		'-f',
		f'sha={sha}',
		token=token,
		check=False,
	)
	if created.returncode == 0:
		return 'created'
	raise SystemExit(
		f'could not push {branch} -> {sha} in {repo} '
		f'(is the executor commit pushed there?): '
		f'{updated.stderr.strip()} / {created.stderr.strip()}'
	)


def open_pr(
	repo: str,
	head: str,
	base: str,
	title: str,
	manager_repo: str,
	manager_pr: str,
	token: str,
) -> str:
	"""Open the executor mirror PR `head -> base`, or reuse one that races in."""
	body = (
		f'Auto-opened executor mirror of {manager_repo}#{manager_pr}.\n\n'
		f'Carries the executor-side work for that manager PR.'
	)
	r = pr_branches_info.gh(
		'pr',
		'create',
		'--repo',
		repo,
		'--base',
		base,
		'--head',
		head,
		'--title',
		title,
		'--body',
		body,
		token=token,
		check=False,
	)
	if r.returncode != 0:
		url = pr_branches_info.existing_pr(repo, head, base, token)
		if url:
			return url
		raise SystemExit(
			f'could not create PR {head} -> {base} in {repo}: {r.stderr.strip()}'
		)
	return r.stdout.strip().splitlines()[-1].strip()


class PrBranches(ci_lib.Tool):
	"""Inspect and provision per-repo branch movement for a manager PR."""

	def name(self) -> str:
		return 'pr-branches'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		sub = parser.add_subparsers(dest='cmd', required=True)
		show = sub.add_parser(
			'show-info',
			help='print each repo base/head/ahead_by/behind_by as JSON',
		)
		show.add_argument('pr', nargs='?', help='manager PR number (default: $PR_NUMBER)')
		prov = sub.add_parser(
			'provision',
			help='force-push each moved executor line mirror branch and open its PR',
		)
		prov.add_argument('pr', nargs='?', help='manager PR number (default: $PR_NUMBER)')
		check = sub.add_parser(
			'check',
			help='fail unless every repo is rebased and every executor pinned commit is landable',
		)
		check.add_argument('pr', nargs='?', help='manager PR number (default: $PR_NUMBER)')

	def show_info(self, args: argparse.Namespace) -> int:
		infos = pr_branches_info.from_env(args.pr)
		print(json.dumps({path: info.as_dict() for path, info in infos.items()}, indent=2))
		return 0

	def provision(self, args: argparse.Namespace) -> int:
		infos = pr_branches_info.from_env(args.pr)
		manager = infos[pr_branches_info.MANAGER_PATH]
		pr_number = args.pr or os.environ['PR_NUMBER']
		manager_token = os.environ['MANAGER_TOKEN']
		executor_token = os.environ['EXECUTOR_TOKEN']

		title = pr_branches_info.gh(
			'api',
			f'repos/{manager.repo}/pulls/{pr_number}',
			'--jq',
			'.title',
			token=manager_token,
		).stdout.strip()

		for info in infos.values():
			if info.line is None:
				continue  # the manager itself; only executor lines are mirrored
			if not self._provisionable(info):
				continue

			state = force_push_head(info.repo, info.head_ref, info.head_sha, executor_token)
			print(f'{info.line}: {state} {info.head_ref} -> {info.head_sha[:12]}')

			# If a PR already exists, only the force-push above is needed; reuse it.
			url = info.pr_url or open_pr(
				info.repo,
				info.head_ref,
				info.base_ref,
				title,
				manager.repo,
				pr_number,
				executor_token,
			)
			print(f'{info.line}: PR {url}')
		return 0

	def check(self, args: argparse.Namespace) -> int:
		"""Gate for the manager PR's CI. Records every problem, then fails if any.

		The manager PR and its executor PRs land almost-atomically via the panel
		(Provision then Merge), so this checks that the land is POSSIBLE, not that the
		executor side already landed:

		1. Every repo (manager + each executor line) must be rebased on its base — a
		repo behind base needs a rebase.
		2. Every executor line that existed at base (base_sha set) must be landable —
		its pinned head commit is present in the executor repo, so the merge can
		squash/push it. Being ahead of base is normal (that is the unlanded work that
		lands with the PR); only a commit missing from the executor repo fails. A line
		the PR adds (base_sha null) is not gated.

		No PR number (a bare manual dispatch not tied to a PR) is a no-op pass —
		there is nothing to gate.
		"""
		pr = args.pr or os.environ.get('PR_NUMBER') or ''
		if not pr.strip():
			print('no PR number provided; nothing to check')
			return 0
		infos = pr_branches_info.from_env(pr)

		errors: list[str] = []

		def record(label: str, check: str, ok: bool, detail: str) -> None:
			# One line per (repo, check): "<repo> <check> OK" or an ::error:: with why.
			if ok:
				print(f'{label} {check} OK')
			else:
				print(f'::error::{label} {check}: {detail}')
				errors.append(f'{label} {check}: {detail}')

		# 1. every repo must be rebased (not behind base)
		for info in infos.values():
			record(
				self._label(info),
				'rebased',
				info.rebased,
				f'{info.behind_by} commit(s) behind base; rebase it',
			)

		# 2. every executor line present at base must be landable: its pinned commit
		# is in the executor repo. Ahead-of-base is fine — it lands with the PR.
		for info in infos.values():
			if info.line is None or info.base_sha is None:
				continue
			detail = (
				'present at base but missing at the PR head'
				if info.head_sha is None
				else f'pinned commit {info.head_sha[:12]} is not on the executor repo; '
				'push/provision it so it can land with the manager PR'
			)
			record(self._label(info), 'landable', info.synced, detail)

		return 1 if errors else 0

	def _label(self, info: RepoInfo) -> str:
		return 'manager' if info.line is None else f'executor {info.line}'

	def _provisionable(self, info: RepoInfo) -> bool:
		"""Whether an executor line has a mirror branch + PR to provision."""
		if info.base_sha is None:
			print(f'{info.line}: base sha is null (line absent at base); skipping')
			return False
		if info.head_sha is None:
			print(f'{info.line}: no head gitlink (line removed); skipping')
			return False
		if not info.moved:
			print(f'{info.line}: gitlink unchanged; skipping')
			return False
		return True

	def handler(self, args: argparse.Namespace) -> int:
		if args.cmd == 'show-info':
			return self.show_info(args)
		if args.cmd == 'provision':
			return self.provision(args)
		if args.cmd == 'check':
			return self.check(args)
		return 1


COMMANDS = [PrBranches()]
