import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

import ci_lib


class CommitHooks(ci_lib.Pipeline):
	"""Run pre-commit hooks for manager and executor repos."""

	def name(self) -> str:
		return 'commit-hooks'

	def handler(self, args: argparse.Namespace) -> int:
		system = ci_lib.output(
			['nix', 'eval', '--impure', '--raw', '--expr', 'builtins.currentSystem']
		).strip()
		repos = [
			('.', 'manager', '.?submodules=1'),
			('executors/v0.3.x', 'executor v0.3.x', '.'),
			('executors/v0.2.x', 'executor v0.2.x', '.'),
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
	"""Check commit messages for changed repos."""

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


class CargoClippy(ci_lib.Pipeline):
	"""Check cargo clippy availability and enumerate crates."""

	def name(self) -> str:
		return 'cargo-clippy'

	def handler(self, args: argparse.Namespace) -> int:
		if ci_lib.run(['cargo', 'clippy', '--version'], check=False).returncode != 0:
			print('ERROR: cargo clippy not installed')
			return 1
		for path in ci_lib.output(['git', 'ls-files']).splitlines():
			if not path.endswith('Cargo.toml'):
				continue
			if path == 'runners/nix/trg/py/modules/genvm-cpython-ext/Cargo.toml':
				continue
			print(f'clippy in {path}')
		return 0


COMMANDS = [CommitHooks(), CommitMessages(), CargoClippy()]
