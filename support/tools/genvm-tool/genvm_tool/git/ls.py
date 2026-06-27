"""`genvm-tool git ls` — tracked files across the manager + executor submodules."""

from .. import common

NAME = 'ls'
HELP = 'list tracked files across the manager and executor submodules'


def configure(parser):
	parser.add_argument(
		'--repo',
		default='all',
		help="'all' (default), 'manager', or an executor name like executors/v0.3.x",
	)


def main(ctx: common.Context, args) -> int:
	drop = common.executor_rels(ctx.root)
	for repo in common.discover_repos(ctx.root, args.repo):
		if not repo.is_checked_out():
			ctx.logger.debug('skipping repo (not checked out)', repo=repo.name)
			continue
		# Only the manager listing can contain submodule gitlinks; drop them.
		drop_rels = drop if repo.rel == '' else None
		for f in common.ls_files(repo, drop_rels):
			print(repo.prefix(f))
	return 0
