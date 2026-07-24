"""`genvm-tool test` — run the test suite.

Everything after `test` is forwarded to the test runner, which has its own
`run` and `show plan|test|services|tags` subcommands plus filter flags. Run
`genvm-tool test run -h` or `genvm-tool test show -h` for those.
"""

import argparse

from . import common

NAME = 'test'
HELP = 'run the language-agnostic test runner'


def configure(parser):
	parser.add_argument(
		'args',
		nargs=argparse.REMAINDER,
		help='arguments forwarded to the test runner (e.g. `run`, `show test`)',
	)


def main(ctx: common.Context, args) -> int:
	# Deferred: only the `test` subcommand depends on aiohttp/jsonnet.
	from .tests.cli import run

	suite = getattr(ctx.project, 'tests', None)
	if suite is None:
		raise common.ToolError(f'{common.PROJECT_FILE} has no `tests` function')

	# The runner drives its own pipeline and calls sys.exit() with the test
	# outcome; a normal return here (it only printed help) means success.
	run(ctx.logger, ctx.printer, args.args, root=ctx.root, suite=suite)
	return 0
