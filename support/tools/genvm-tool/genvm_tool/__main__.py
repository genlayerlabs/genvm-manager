#!/usr/bin/env python3

"""Command-line interface for genvm-tool.

Top-level commands (`configure`, `test`) sit alongside command *groups* (`git`
with `git ls` / `git list-repo`, `hook` with `hook run` / `hook install`). Each group is a
sub-package exposing `COMMANDS`; every leaf module exposes `NAME`, `HELP`,
`configure(parser)` and `main(ctx, args)` (sync or async).
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import shtab

from . import (
	cmd_build_manifest,
	cmd_codegen,
	cmd_configure,
	cmd_test,
	common,
	formatter,
	git,
	hook,
)

TOPLEVEL = [cmd_configure, cmd_test, cmd_build_manifest, cmd_codegen]
GROUPS = [git, hook]


def _add_leaf(subparsers, mod) -> None:
	sub = subparsers.add_parser(mod.NAME, help=mod.HELP)
	mod.configure(sub)
	sub.set_defaults(func=mod.main)


def _create_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		prog='genvm-tool',
		description='GenVM monorepo git helper (build, tests, hooks, ls)',
	)
	# `--print-completion {bash,zsh,tcsh}` emits a shell completion script for the
	# whole command tree and exits (shtab walks the subparsers below at call time).
	shtab.add_argument_to(parser, '--print-completion')
	parser.add_argument(
		'-C', '--chdir', help='change working directory before doing anything'
	).complete = shtab.DIRECTORY
	parser.add_argument(
		'--log-format', choices=['text', 'json'], default='text', help='log format'
	)
	parser.add_argument(
		'--log-level',
		choices=['trace', 'debug', 'info', 'warning', 'error'],
		default='info',
		help='logging level',
	)

	subparsers = parser.add_subparsers(dest='command')
	for mod in TOPLEVEL:
		_add_leaf(subparsers, mod)
	for grp in GROUPS:
		gp = subparsers.add_parser(grp.NAME, help=grp.HELP)
		leaves = gp.add_subparsers(dest=f'{grp.NAME}_command')
		for mod in grp.COMMANDS:
			_add_leaf(leaves, mod)
	return parser


def main() -> None:
	sys.dont_write_bytecode = True

	parser = _create_parser()
	args = parser.parse_args()

	stdout = formatter.DefaultLockableTextIO(sys.stdout)
	stderr = formatter.DefaultLockableTextIO(sys.stderr)
	match args.log_format:
		case 'json':
			logger = formatter.JsonFormatter(stderr)
			printer = formatter.JsonFormatter(stdout)
		case _:
			logger = formatter.TextFormatter(stderr)
			printer = formatter.TextFormatter(stdout)
	logger.min_level = formatter.Level.from_str(args.log_level)

	if args.chdir:
		os.chdir(Path(args.chdir).absolute())

	if not getattr(args, 'func', None):
		parser.print_help()
		sys.exit(1)

	try:
		root = common.find_root()

		env_file = root / '.env'
		if env_file.exists():
			from dotenv import load_dotenv

			load_dotenv(env_file)

		python_command = [sys.executable, '-B']

		# Pre-subcommand: exec the manager's `.genvm-tool.py` so subcommands can
		# ask it for the test suite (`tests`) and the manager's commit hooks.
		ctx = common.Context(
			root=root,
			logger=logger,
			printer=printer,
			python_command=python_command,
			project=common.load_project(root),
		)
		func = args.func
		rc = (
			asyncio.run(func(ctx, args))
			if asyncio.iscoroutinefunction(func)
			else func(ctx, args)
		)
	except common.ToolError as e:
		logger.error(str(e))
		sys.exit(1)
	sys.exit(rc)


if __name__ == '__main__':
	main()
