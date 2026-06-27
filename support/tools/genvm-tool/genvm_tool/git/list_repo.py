"""`genvm-tool git list-repo` — list the manager + executor submodule repos."""

from .. import common

NAME = 'list-repo'
HELP = 'list the manager and every executor submodule repo'


def configure(parser):
	parser.add_argument(
		'--repo',
		default='all',
		help="'all' (default), 'manager', or an executor name like executors/v0.3.x",
	)


def main(ctx: common.Context, args) -> int:
	for repo in common.discover_repos(ctx.root, args.repo):
		ctx.printer.put(
			repo.name,
			path=repo.rel or '.',
			checked_out=repo.is_checked_out(),
		)
	return 0
