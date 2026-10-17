import argparse
import contextlib
import sys
import types
import typing
from pathlib import Path

import genvm_tool.tests
from genvm_tool.tests import SharedContext

type Collector = typing.Callable[['genvm_tool.tests.stage.collection.Context'], None]
type Reporter = typing.Callable[
	['genvm_tool.tests.SharedContext', 'genvm_tool.tests.stage.execution.Env'], None
]


def eval_module(file: Path, root_dir: Path) -> types.ModuleType:
	"""
	Compile and exec ``file`` as a throwaway module, returning it.

	The module is registered in ``sys.modules`` (under a dotted name derived from
	its path relative to ``root_dir``) so its own relative imports resolve. Used
	both for evaluated suite files and for per-directory ``test.py`` definitions.
	"""
	rel_path = file.relative_to(root_dir)
	as_module = rel_path.with_suffix('').as_posix().replace('/', '.')
	module = types.ModuleType(as_module)
	module.__dict__['__file__'] = str(file.absolute())
	compiled = compile(file.read_text(), str(file.absolute()), 'exec')
	exec(compiled, module.__dict__)
	sys.modules[as_module] = module
	return module


class Context:
	shared: SharedContext
	parser: argparse.ArgumentParser
	run_parser: argparse.ArgumentParser
	filter_parser: argparse.ArgumentParser

	_collectors: list[Collector]
	_reporter: list[Reporter]

	def add_reporter(self, step: Reporter) -> None:
		"""Register a step to run after test execution completes."""
		self._reporter.append(step)

	def add_collector(self, collector: Collector) -> None:
		self._collectors.append(collector)


_GLOBAL_CTX: Context | None = None


def current_context() -> Context:
	if _GLOBAL_CTX is None:
		raise RuntimeError('No global context is set')
	return _GLOBAL_CTX


@contextlib.contextmanager
def with_context(ctx: Context) -> typing.Generator[Context, None, None]:
	global _GLOBAL_CTX
	old_ctx = _GLOBAL_CTX
	try:
		_GLOBAL_CTX = ctx
		yield ctx
	finally:
		_GLOBAL_CTX = old_ctx


class Env(typing.NamedTuple):
	args: argparse.Namespace
	collectors: list[Collector]
	post_run_steps: list[Reporter]


class InitialEnv(typing.NamedTuple):
	parser: argparse.ArgumentParser
	run_parser: argparse.ArgumentParser
	filter_parser: argparse.ArgumentParser
	remaining_args: list[str]


def run(shared: SharedContext, env: InitialEnv, /) -> Env:
	ctx = Context()
	ctx.shared = shared
	ctx.parser = env.parser
	ctx.run_parser = env.run_parser
	ctx.filter_parser = env.filter_parser
	ctx._collectors = []
	ctx._reporter = []
	if shared.suite is None:
		raise RuntimeError('no test suite configured (.genvm-tool.py:tests missing)')
	with with_context(ctx) as ctx:
		# Plugins grab the active context via `current_context()` at import time,
		# so the suite must run inside it.
		shared.suite(ctx)

	return Env(
		args=ctx.parser.parse_args(env.remaining_args),
		collectors=ctx._collectors,
		post_run_steps=ctx._reporter,
	)
