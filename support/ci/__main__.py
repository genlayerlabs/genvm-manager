import argparse
import sys
import typing

import ci_lib
from pipelines import build, checks, docs, tests
from tools import branches, script_adapters, versions

PIPELINES: list[ci_lib.Pipeline] = [
	*build.COMMANDS,
	*docs.COMMANDS,
	*tests.COMMANDS,
	*checks.COMMANDS,
]
TOOLS: list[ci_lib.Tool] = [
	*versions.COMMANDS,
	*branches.COMMANDS,
	*script_adapters.COMMANDS,
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
	sys.exit(main())
