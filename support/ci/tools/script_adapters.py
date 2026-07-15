import argparse
import importlib
import sys

import ci_lib


class ModuleTool(ci_lib.Tool):
	def __init__(self, name: str, module: str):
		self._name = name
		self._module = module

	def name(self) -> str:
		return self._name

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		parser.add_argument('args', nargs=argparse.REMAINDER)

	def handler(self, args: argparse.Namespace) -> int:
		module = importlib.import_module(self._module)
		old_argv = sys.argv
		try:
			sys.argv = [self._name, *args.args]
			try:
				result = module.main()
			except SystemExit as exc:
				if exc.code is None:
					return 0
				if isinstance(exc.code, int):
					return exc.code
				print(exc.code, file=sys.stderr)
				return 1
		finally:
			sys.argv = old_argv
		return result or 0


COMMANDS = [
	ModuleTool('genvm-merge-into-dev', 'tools.genvm_merge_into_dev'),
	ModuleTool('make-release-notes', 'tools.make_release_notes'),
	ModuleTool('make-relase-notes', 'tools.make_release_notes'),
	ModuleTool('open-executor-prs', 'tools.open_executor_prs'),
	ModuleTool('pr-action-panel', 'tools.pr_action_panel'),
]
