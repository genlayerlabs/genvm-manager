#!/usr/bin/env python3

"""Command-line interface for genvm-tool.

Top-level commands (`configure`, `test`) sit alongside command *groups* (`git`
with `git ls` / `git list-repo`). Each group is a sub-package exposing
`COMMANDS`; every leaf module exposes `NAME`, `HELP`, `configure(parser)` and
`main(ctx, args)` (sync or async). Commit hooks are handled by git-hooks.nix
(each repo's flake), not this tool — see `support/ci/run.sh pipeline commit-hooks`.
"""

import argparse
import asyncio
import inspect
import os
import re
import sys
from pathlib import Path

import shtab

from . import (
	cmd_build_manifest,
	cmd_codegen,
	cmd_configure,
	cmd_howto,
	cmd_test,
	common,
	formatter,
	git,
)

TOPLEVEL = [cmd_configure, cmd_test, cmd_build_manifest, cmd_codegen, cmd_howto]
GROUPS = [git]

# One-line tagline for the man page NAME line and completion header; the fuller
# `description=` below feeds the man page DESCRIPTION section and top-level -h.
TAGLINE = 'GenVM monorepo build/test/codegen/git helper'
DESCRIPTION = """\
Multi-repo helper for the GenVM monorepo. It configures the ninja build graph,
runs the language-agnostic test suite, generates code and manifests, and drives
git across the manager and its executor submodules.

Commands are either top-level leaves (configure, test, ...) or a group with its
own leaves (git ls, git check-for-push, ...). Pass -h to any of them for the
per-command details, or run `genvm-tool howto` for the contributor guides."""
EPILOG = (
	'Guides live in docs/contributing/howto/ (rendered by `genvm-tool howto`). '
	'Every subcommand accepts -h/--help.'
)


# The module docstrings double as Sphinx-style source docs, so they carry RST
# inline markup (`:class:`~pkg.Name`` cross-refs, ``literal`` code spans). Render
# it to plain text so a man page / `-h` reader doesn't see the markup verbatim.
# The optional first `:domain:` covers explicit-domain roles like `:py:class:`.
_RST_ROLE = re.compile(r':(?:[a-z]+:)?[a-z]+:`(~)?([^`]+)`')
_RST_LITERAL = re.compile(r'``([^`]+)``')


def _rst_to_plain(text: str) -> str:
	# A leading `~` in a cross-ref means "show only the last dotted segment".
	text = _RST_ROLE.sub(
		lambda m: m.group(2).rsplit('.', 1)[-1] if m.group(1) else m.group(2), text
	)
	return _RST_LITERAL.sub(r'`\1`', text)


def _long_help(mod):
	"""The module docstring as a subparser `description` (falls back to HELP).

	`inspect.getdoc` dedents and strips; `_rst_to_plain` drops the RST markup so
	the rich per-command docstrings feed both the subcommand's `-h` and the man
	page's COMMAND section as clean prose.
	"""
	return _rst_to_plain(inspect.getdoc(mod) or mod.HELP)


def _add_leaf(subparsers, mod) -> None:
	sub = subparsers.add_parser(
		mod.NAME,
		help=mod.HELP,
		description=_long_help(mod),
		formatter_class=argparse.RawDescriptionHelpFormatter,
	)
	mod.configure(sub)
	sub.set_defaults(func=mod.main)


class _PrintManpageAction(argparse.Action):
	"""`--print-manpage`: emit a roff man page for the whole tree, then exit.

	The manager's `default.nix` pipes this into `installManPage` at build time,
	mirroring how shtab's `--print-completion` feeds `installShellCompletion`.
	`argparse_manpage` walks the same subparser tree shtab does, turning each
	command's `description=` into its COMMAND section.
	"""

	def __init__(self, option_strings, dest, **kwargs):
		super().__init__(
			option_strings, dest, nargs=0, help='print a man page and exit', **kwargs
		)

	def __call__(self, parser, namespace, values, option_string=None):
		import importlib.metadata

		from argparse_manpage.manpage import Manpage

		try:
			version = importlib.metadata.version('genvm-tool')
		except importlib.metadata.PackageNotFoundError:
			version = 'dev'
		# Build a fresh, *full* tree (with the `test` runner subtree grafted in) so
		# the man page documents `test run` / `test show ...`, which the dispatch
		# parser hides behind argparse.REMAINDER.
		full = _create_parser(full=True)
		data = {
			'prog': full.prog,
			'description': TAGLINE,
			'version': version,
			'project_name': full.prog,
			'manual_section': '1',
		}
		sys.stdout.write(str(Manpage(full, format='pretty', _data=data)))
		parser.exit()


def _create_parser(full: bool = False) -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		prog='genvm-tool',
		description=DESCRIPTION,
		epilog=EPILOG,
		formatter_class=argparse.RawDescriptionHelpFormatter,
	)
	# `--print-completion {bash,zsh,tcsh}` emits a shell completion script for the
	# whole command tree and exits (shtab walks the subparsers below at call time).
	shtab.add_argument_to(parser, '--print-completion')
	# `--print-manpage` does the analogous thing for the man page (see the action).
	parser.add_argument('--print-manpage', action=_PrintManpageAction)
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
		gp = subparsers.add_parser(
			grp.NAME,
			help=grp.HELP,
			description=_long_help(grp),
			formatter_class=argparse.RawDescriptionHelpFormatter,
		)
		leaves = gp.add_subparsers(dest=f'{grp.NAME}_command')
		for mod in grp.COMMANDS:
			_add_leaf(leaves, mod)

	if full:
		# Dispatch keeps `test` as an argparse.REMAINDER leaf so the aiohttp/jsonnet
		# runner closure stays lazy (see cmd_test). For docs we want its real
		# `run` / `show ...` subtree, so import the runner's own parser (it needs no
		# root or suite) and splice it into the `test` slot for the parser walk.
		from .tests.cli import create_parser as _test_create_parser

		subparsers.choices['test'] = _test_create_parser().parser

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
	import subprocess

	try:
		main()
	except subprocess.CalledProcessError as e:
		import shlex
		import traceback

		traceback.print_exc()
		print(' '.join(shlex.quote(a) for a in e.cmd), file=sys.stderr)
		print(e.stdout, file=sys.stdout)
		print(e.stderr, file=sys.stderr)
		sys.exit(1)
