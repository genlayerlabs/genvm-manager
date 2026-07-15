import argparse
import os
import subprocess

import ci_lib

from tools.versions import active_versions


def gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
	return ci_lib.run(['gh', *args], check=check, capture_output=True)


class ProvisionBranches(ci_lib.Tool):
	"""Provision version/dev branches and standing release-gate PRs."""

	def name(self) -> str:
		return 'provision-branches'

	def remote_has(self, branch: str) -> bool:
		return (
			ci_lib.run(
				['git', 'show-ref', '--verify', '--quiet', f'refs/remotes/origin/{branch}'],
				check=False,
			).returncode
			== 0
		)

	def handler(self, args: argparse.Namespace) -> int:
		ci_lib.run(['git', 'fetch', 'origin', '--prune'])

		pushrefs: list[str] = []
		for version in active_versions():
			version_branch = f'v{version}.x'
			dev_branch = f'v{version}-dev'

			if not self.remote_has(version_branch):
				print(f'Will create version branch {version_branch} from main')
				version_commit = ci_lib.output(['git', 'rev-parse', 'origin/main']).strip()
				pushrefs.append(f'origin/main:refs/heads/{version_branch}')
			else:
				print(f'Version branch {version_branch} already exists')
				version_commit = ci_lib.output(
					['git', 'rev-parse', f'origin/{version_branch}']
				).strip()

			if not self.remote_has(dev_branch):
				print(
					f'Will create dev branch {dev_branch} from {version_branch} (with seed commit)'
				)
				seed = ci_lib.output(
					[
						'git',
						'commit-tree',
						f'{version_commit}^{{tree}}',
						'-p',
						version_commit,
						'-m',
						f'chore(ci): seed {dev_branch} release-gate branch 🌱',
					],
					cwd=ci_lib.ROOT_DIR,
				).strip()
				pushrefs.append(f'{seed}:refs/heads/{dev_branch}')
			else:
				print(f'Dev branch {dev_branch} already exists')

		if pushrefs:
			print(f'Creating {len(pushrefs)} branch(es) in one atomic push')
			ci_lib.run(['git', 'push', '--atomic', 'origin', *pushrefs])
			ci_lib.run(['git', 'fetch', 'origin', '--prune'])
		else:
			print('All branches already exist; nothing to push')

		repo = os.environ['GITHUB_REPOSITORY']
		for version in active_versions():
			version_branch = f'v{version}.x'
			dev_branch = f'v{version}-dev'
			existing = gh(
				'pr',
				'list',
				'--repo',
				repo,
				'--base',
				version_branch,
				'--head',
				dev_branch,
				'--state',
				'open',
				'--json',
				'number',
				'--jq',
				'.[0].number // ""',
			).stdout.strip()
			if existing:
				print(
					f'Standing PR {dev_branch} -> {version_branch} already open (#{existing})'
				)
				continue

			print(f'Opening standing release-gate PR {dev_branch} -> {version_branch}')
			gh(
				'pr',
				'create',
				'--repo',
				repo,
				'--base',
				version_branch,
				'--head',
				dev_branch,
				'--title',
				f'Release gate: {dev_branch} -> {version_branch}',
				'--body',
				f'Standing release-gate PR. `{dev_branch}` accumulates incremental v{version} work; this PR merges into the release-ready `{version_branch}` branch only once the cross-repo E2E pipeline is green. Comment `/run-e2e v{version}` to fire it (genlayerlabs/genlayer-e2e). It may stay red while the v{version} train is in progress.',
			)
		return 0


COMMANDS = [ProvisionBranches()]
