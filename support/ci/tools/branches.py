import argparse

import ci_lib
import gh_common

from tools.versions import active_versions


def gh(*args: str, check: bool = True) -> str:
	"""
	`gh` with the manager token, returning stdout.

	Shares gh_common's wrapper rather than shelling out separately, so this tool
	gets the same tracing, token resolution and transient-failure retries as
	every other one. `retry=False`: the calls below include `pr create`, which
	must not be replayed.
	"""
	return gh_common.gh_manager(*args, check=check, retry=False)


class ProvisionBranches(ci_lib.Tool):
	"""
	Provision version/dev branches and standing release-gate PRs.
	"""

	def name(self) -> str:
		return 'provision-branches'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		# Only the manager repo is relevant here — it iterates executor lines from
		# .genvm-monorepo-root, not a single PR/head.
		gh_common.add_args(parser, executor_repo=False, pr=False, head_ref=False)

	def remote_has(self, branch: str) -> bool:
		return (
			ci_lib.run(
				['git', 'show-ref', '--verify', '--quiet', f'refs/remotes/origin/{branch}'],
				check=False,
			).returncode
			== 0
		)

	def handler(self, args: argparse.Namespace) -> int:
		repo = gh_common.Ctx.from_args(args).manager_repo
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
			).strip()
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
				# A conventional-commit subject: the title is what `pr-title` and the
				# Merge action validate, and it lands verbatim as a squashed subject.
				f'chore(release): gate {dev_branch} into {version_branch} 🚀',
				'--body',
				f'Standing release-gate PR. `{dev_branch}` accumulates incremental v{version} work; this PR merges into the release-ready `{version_branch}` branch only once the cross-repo E2E pipeline is green. Comment `/run-e2e v{version}` to fire it (genlayerlabs/genlayer-e2e). It may stay red while the v{version} train is in progress.',
			)
		return 0


COMMANDS = [ProvisionBranches()]
