"""`genvm-tool hook run` — run per-repo hooks (CI, manual, and git-hook entry).

This is also the git-hook entry point: the installed stubs invoke
`hook run --repo <repo> --hook-stage <stage> -- "$@"`, where the trailing args
carry e.g. the commit-msg file path. Without `--all-files` only staged files
are linted; a manager commit stages only manager files (the executor gitlink is
dropped), an executor commit only executor files. The manager's `pre-commit`
stub selects `--repo all`, so a manager commit also fans out to the children's
`pre-commit` over their respective staged files.

Hooks fix in place by default (a tool with a `fix_args` invocation rewrites;
others fall back to their check invocation). Pass `--check` to only verify and
never modify — CI uses it so unformatted code fails instead of being silently
rewritten.

Repos run concurrently (disjoint working trees); each repo builds its tool
profile then runs its hooks sequentially.
"""

import asyncio

from .. import common
from . import engine

NAME = 'run'
HELP = 'run hooks across the manager and executor submodules'


def configure(parser):
	parser.add_argument('--repo', default='all', help='repo selector (default: all)')
	parser.add_argument(
		'--all-files',
		action='store_true',
		help='run over every tracked file instead of just the staged ones',
	)
	# Fix in place by default; tools without a fix mode just run as a check.
	# `--check` forces check-only (CI uses it so unformatted code fails instead
	# of being silently rewritten).
	fix = parser.add_mutually_exclusive_group()
	fix.add_argument(
		'--fix',
		dest='fix',
		action='store_true',
		default=True,
		help='apply fixes in place where the tool supports it (default)',
	)
	fix.add_argument(
		'--check',
		dest='fix',
		action='store_false',
		help='only check; never modify files',
	)
	parser.add_argument('--hook-stage', default=common.DEFAULT_STAGE)
	# Trailing args git passes to the hook (e.g. the commit-msg file path).
	parser.add_argument('hook_args', nargs='*')


async def _one_repo(ctx, repo, stage, all_files, msg_file, fix):
	if not engine.load_hooks(ctx, repo, stage):
		return []
	if not repo.is_checked_out():
		raise common.ToolError(f'{repo.name} is not checked out (submodule missing)')
	if stage == 'commit-msg':
		files = []
	elif all_files:
		# Drop submodule gitlinks from the manager listing (they are not files).
		drop = common.executor_rels(ctx.root) if repo.rel == '' else None
		files = common.ls_files(repo, drop)
	else:
		files = common.staged_files(repo)
	return await engine.process_repo(ctx, repo, stage, files, msg_file=msg_file, fix=fix)


async def main(ctx: common.Context, args) -> int:
	repos = common.discover_repos(ctx.root, args.repo)
	msg_file = (
		args.hook_args[0] if (args.hook_stage == 'commit-msg' and args.hook_args) else None
	)
	batches = await asyncio.gather(
		*(
			_one_repo(ctx, r, args.hook_stage, args.all_files, msg_file, args.fix)
			for r in repos
		)
	)
	failed = [res for batch in batches for res in batch if res.status == 'fail']
	if failed:
		ctx.printer.put(
			'hooks failed',
			count=len(failed),
			hooks=[f'{r.repo}:{r.hook_id}' for r in failed],
		)
		return 1
	return 0
