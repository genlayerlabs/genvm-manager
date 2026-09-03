import argparse
import subprocess
import sys
import typing

import ci_lib
from pipelines import build, checks, docs, release, tests
from tools import (
	branches,
	deploy_docs,
	full_tests_command,
	make_release_notes,
	open_executor_prs,
	pr_action_panel,
	pr_branches,
	rebase_watch,
	sync_executor_branches,
	versions,
)

PIPELINES: list[ci_lib.Pipeline] = [
	*build.COMMANDS,
	*docs.COMMANDS,
	*tests.COMMANDS,
	*checks.COMMANDS,
	*release.COMMANDS,
]
TOOLS: list[ci_lib.Tool] = [
	*versions.COMMANDS,
	*branches.COMMANDS,
	*deploy_docs.COMMANDS,
	*full_tests_command.COMMANDS,
	*pr_branches.COMMANDS,
	*make_release_notes.COMMANDS,
	*pr_action_panel.COMMANDS,
	*open_executor_prs.COMMANDS,
	*rebase_watch.COMMANDS,
	*sync_executor_branches.COMMANDS,
]


def register_commands(
	subparsers: argparse._SubParsersAction,
	commands: typing.Sequence[ci_lib.Command],
) -> None:
	for command in commands:
		sub = subparsers.add_parser(command.name(), help=command.__doc__)
		command.add_to(sub)
		sub.set_defaults(func=command.handler)


def main() -> int:
	parser = argparse.ArgumentParser(description='Run CI pipelines and tools')
	top = parser.add_subparsers(dest='category', required=True)

	pipeline = top.add_parser('pipeline', help='Run a CI pipeline')
	register_commands(pipeline.add_subparsers(dest='pipeline', required=True), PIPELINES)

	tool = top.add_parser('tool', help='Run a CI support tool')
	register_commands(tool.add_subparsers(dest='tool', required=True), TOOLS)

	args = parser.parse_args()
	return args.func(args) or 0


if __name__ == '__main__':
	try:
		sys.exit(main())
	except subprocess.CalledProcessError as e:
		import shlex
		import traceback

		traceback.print_exc()
		print(' '.join(shlex.quote(a) for a in e.cmd), file=sys.stderr)
		print(e.stdout, file=sys.stdout)
		print(e.stderr, file=sys.stderr)
		sys.exit(1)
