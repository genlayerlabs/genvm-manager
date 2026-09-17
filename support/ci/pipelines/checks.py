import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import behind
import ci_lib
import gh_common
from tools.versions import active_lines


class CommitHooks(ci_lib.Pipeline):
	"""
	Run pre-commit hooks for manager and executor repos.
	"""

	def name(self) -> str:
		return 'commit-hooks'

	def handler(self, args: argparse.Namespace) -> int:
		system = ci_lib.output(
			['nix', 'eval', '--impure', '--raw', '--expr', 'builtins.currentSystem']
		).strip()
		# Derived from .genvm-monorepo-root, never listed here: a hardcoded list
		# silently skips a newly added line's hooks and breaks on a removed one.
		# Newest line first, so the manager's own train is exercised early.
		repos = [('.', 'manager', '.?submodules=1')] + [
			(f'executors/{line}.x', f'executor {line}.x', '.')
			for line in reversed(active_lines())
		]
		for directory, label, flakeref in repos:
			with ci_lib.github_group(f'pre-commit: {label}'):
				cwd = ci_lib.ROOT_DIR / directory
				cfg = ci_lib.output(
					[
						'nix',
						'build',
						'--no-link',
						'--print-out-paths',
						f'{flakeref}#checks.{system}.pre-commit-check.config.configFile',
					],
					cwd=cwd,
				).strip()
				pc = (
					Path(
						ci_lib.output(
							[
								'nix',
								'build',
								'--no-link',
								'--print-out-paths',
								f'{flakeref}#checks.{system}.pre-commit-check.config.package',
							],
							cwd=cwd,
						).strip()
					)
					/ 'bin'
					/ 'pre-commit'
				)
				with tempfile.TemporaryDirectory() as home:
					ci_lib.run(
						[
							pc,
							'run',
							'--all-files',
							'--config',
							cfg,
							'--hook-stage',
							'pre-commit',
							'--show-diff-on-failure',
							'--color',
							'always',
						],
						cwd=cwd,
						env={'PRE_COMMIT_HOME': home},
					)
		return 0


class CommitMessages(ci_lib.Pipeline):
	"""
	Check commit messages for changed repos.
	"""

	def name(self) -> str:
		return 'commit-messages'

	def handler(self, args: argparse.Namespace) -> int:
		if not os.environ.get('CHANGES'):
			ci_lib.github_error(
				"CHANGES is empty; it must hold the get-src action's `changes` output"
			)
			return 1

		rc = 0
		for directory, info in json.loads(os.environ['CHANGES']).items():
			if not info.get('has_changes'):
				continue
			base = info.get('base_commit')
			head = info.get('branch_commit')
			if not base or not head:
				continue

			label = 'manager' if directory == '.' else directory
			checker = (
				ci_lib.ROOT_DIR / directory / 'support' / 'scripts' / 'check-commit-message.py'
			)
			if not checker.is_file():
				ci_lib.github_error(f'{label} has no support/scripts/check-commit-message.py')
				rc = 1
				continue

			if (
				ci_lib.run(
					['git', '-C', directory, 'cat-file', '-e', f'{base}^{{commit}}'], check=False
				).returncode
				!= 0
			):
				ci_lib.run(
					[
						'git',
						'-C',
						directory,
						'fetch',
						'--no-tags',
						'--force',
						'origin',
						'+refs/heads/*:refs/remotes/origin/*',
					]
				)

			with ci_lib.github_group(f'commit messages: {label} ({base}..{head})'):
				commits = ci_lib.output(
					['git', '-C', directory, 'rev-list', f'{base}..{head}']
				).split()
				if not commits:
					print('no new commits')
				for sha in commits:
					subject = ci_lib.output(
						['git', '-C', directory, 'log', '-1', '--format=%s', sha]
					).strip()
					message = ci_lib.output(
						['git', '-C', directory, 'log', '-1', '--format=%B', sha]
					)
					result = ci_lib.run(
						[sys.executable, checker, '--message-text', message], check=False
					)
					if result.returncode == 0:
						print(f'ok   {sha} {subject}')
					else:
						ci_lib.github_error(f'{label} {sha}: bad commit message: {subject}')
						rc = 1
		return rc


CHECKER = Path('support') / 'scripts' / 'check-commit-message.py'


def pr_field(pr: str, field: str) -> str:
	"""
	One field of a PR, read from the API.

	The PR-scoped checks must not depend on `github.event.pull_request.*`: that
	payload is absent on the panel's `workflow_dispatch`, which is how they came
	to skip on exactly the runs the App accepts. A PR number is available
	on every triggering event, so everything else is resolved from it here.
	"""
	return gh_common.gh_manager(
		'api', f'repos/{gh_common.repo()}/pulls/{pr}', '--jq', f'.{field}'
	).strip()


class BehindCheck(ci_lib.Pipeline):
	"""
	Fail unless the PR head already contains its base tip (0 commits behind).
	"""

	def name(self) -> str:
		return 'behind-check'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		gh_common.add_args(parser, executor_repo=False, head_ref=False)

	def handler(self, args: argparse.Namespace) -> int:
		ctx = gh_common.Ctx.from_args(args)
		gh_common.set_ctx(ctx)
		pr = ctx.pr_number_opt
		if not pr:
			ci_lib.github_error('PR must be set (the PR number to check)')
			return 1
		base = os.environ.get('BASE', '') or pr_field(pr, 'base.ref')
		if not base:
			ci_lib.github_error(f'could not resolve the base branch of PR #{pr}')
			return 1

		# Resolve both sides by explicit refspec so this also works for fork PRs,
		# whose head sha is not fetchable on its own.
		ci_lib.run(
			[
				'git',
				'fetch',
				'--no-tags',
				'--no-recurse-submodules',
				'origin',
				f'+refs/heads/{base}:refs/base',
				f'+refs/pull/{pr}/head:refs/prhead',
			]
		)

		count = behind.behind_by_git('refs/base', 'refs/prhead')
		if count:
			ci_lib.github_error(
				f'PR is {count} commit(s) behind {base}; update/rebase the branch so it '
				f'is 0 behind before merging'
			)
			return 1
		print(f'0 commits behind {base}')
		return 0


class PrTitle(ci_lib.Pipeline):
	"""
	Fail unless the PR title is a valid conventional-commit subject.

	The E2E App squashes a PR into `<title> (#N)`, so the title becomes a
	commit subject verbatim — and nothing re-validates a generated squash
	message against the commit-message hook. A title that would not pass as a
	commit subject therefore lands as a commit the hook rejects. Checking it on
	the PR moves that failure to where it can still be fixed.
	"""

	def name(self) -> str:
		return 'pr-title'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		gh_common.add_args(parser, executor_repo=False, head_ref=False)

	def handler(self, args: argparse.Namespace) -> int:
		ctx = gh_common.Ctx.from_args(args)
		gh_common.set_ctx(ctx)
		pr = (ctx.pr_number_opt or '').strip()
		if not pr:
			ci_lib.github_error('PR must be set (the PR number to check)')
			return 1
		# From the API, not `github.event.pull_request.title`, so this also runs
		# on a panel-dispatched run — see `pr_field`.
		title = os.environ.get('PR_TITLE', '') or pr_field(pr, 'title')
		if not title.strip():
			ci_lib.github_error(f'could not resolve the title of PR #{pr}')
			return 1

		# Validate the subject that will actually LAND, not the bare title: the
		# GitHub squash appends ` (#N)`, and the
		# checker measures the raw subject length. Checking the title alone would
		# pass a title just under the limit and still land an over-long subject.
		subject = f'{title} (#{pr})'

		# Subject-only: the same validator the commit-msg hook runs, which treats
		# a single-line message as a bare subject (format, emoji placement,
		# length, AI attribution).
		result = ci_lib.run(
			[sys.executable, ci_lib.ROOT_DIR / CHECKER, '--message-text', subject],
			check=False,
		)
		if result.returncode != 0:
			ci_lib.github_error(
				f'PR title is not a valid commit subject: {subject!r}. The E2E App '
				f'lands this as the squashed commit subject, so it must satisfy '
				f'the same rules.'
			)
			return 1
		print(f'ok   {subject}')
		return 0


SUMMARY_DIFF_LIMIT = 512 * 1024


def _worktree_diff() -> str:
	# `--submodule=diff`: executor crates live in submodules, whose changes would
	# otherwise collapse into an unusable "dirty gitlink" line.
	return ci_lib.output(['git', 'diff', '--submodule=diff'])


def _summary_patch(diff: str) -> str:
	# GitHub caps a job summary at 1 MiB and drops the whole file past it; cut on
	# a line boundary so what is shown stays an appliable patch, and the log above
	# always carries the full one.
	encoded = diff.encode()
	if len(encoded) <= SUMMARY_DIFF_LIMIT:
		return f'```diff\n{diff}```\n'
	# The cap is on bytes, and a cut can land inside a multi-byte character.
	cut = encoded[:SUMMARY_DIFF_LIMIT].decode(errors='ignore')
	shown = cut.rsplit('\n', 1)[0]
	return f'```diff\n{shown}\n```\n\nPatch truncated, see the job log.\n'


class CargoClippy(ci_lib.Pipeline):
	"""
	Lint every registered crate, reporting a failure as an appliable patch.

	Clippy names the lint but not the edit. On failure the `--fix` edges rerun
	over the same tree and the resulting diff is printed and put in the job
	summary, so the author can apply it verbatim; lints `--fix` cannot rewrite
	stay visible in the diagnostics above it.

	Must run in a dev shell carrying the pinned toolchain — clippy is a component
	of it, and the linted crates must be the ones the rest of CI builds.
	"""

	def name(self) -> str:
		return 'cargo-clippy'

	def handler(self, args: argparse.Namespace) -> int:
		# Lint edges declare no inputs, so nothing regenerates sources for them; a
		# drifted generated file would be linted as if it were current.
		with ci_lib.github_group('codegen'):
			ci_lib.run(['ninja', '-C', 'build', 'codegen'])

		# Anything dirty here is not clippy's doing and must not be presented as
		# its patch below.
		pre_existing = ci_lib.output(['git', 'diff', '--name-only']).split()

		# `-k 0`: report every crate's diagnostics in one run, not just the first
		# failing one.
		with ci_lib.github_group('clippy'):
			ok = (
				ci_lib.run(
					['ninja', '-k', '0', '-C', 'build', 'cargo/clippy'], check=False
				).returncode
				== 0
			)
		if ok:
			return 0

		with ci_lib.github_group('suggested patch'):
			if pre_existing:
				print(
					'the worktree was already dirty before the fix run, so the patch below '
					f'also carries unrelated changes to: {" ".join(pre_existing)}'
				)
			ci_lib.run(['ninja', '-k', '0', '-C', 'build', 'cargo/clippy/fix'], check=False)
			diff = _worktree_diff()
			if diff.strip():
				print(diff)
				if pre_existing:
					ci_lib.github_step_summary(
						'### `cargo clippy` failed\n\n'
						'Patch omitted because the worktree was already dirty before the fix run.'
					)
				else:
					ci_lib.github_step_summary(
						'### `cargo clippy` failed\n\n'
						'Machine-applicable part of the fix:\n\n' + _summary_patch(diff)
					)
			else:
				print('`cargo clippy --fix` rewrote nothing; fix the diagnostics by hand')

		ci_lib.github_error(
			'cargo clippy failed; see the diagnostics and the suggested patch above'
		)
		return 1


COMMANDS = [CommitHooks(), CommitMessages(), BehindCheck(), PrTitle(), CargoClippy()]
