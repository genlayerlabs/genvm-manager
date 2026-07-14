"""`genvm-tool test` — the language-agnostic test runner.

Everything after `test` is forwarded to the runner under `genvm_tool/tests/`:
its `run` / `show plan|test|services|tags` subcommands plus the suite-provided
filter flags. The runner reuses genvm-tool's `Context` (logger/printer) and the
top-level `-C` / `--log-format` / `--log-level`; the runner package is imported
lazily so the stdlib-only paths (`configure`, `git ls`) never pull in its
`aiohttp` / `jsonnet` dependency closure.
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
