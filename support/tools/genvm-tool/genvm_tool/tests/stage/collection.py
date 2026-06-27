from __future__ import annotations

import argparse
import typing
from dataclasses import dataclass, field
from typing import NamedTuple

import genvm_tool.tests
from genvm_tool.tests import SharedContext, const

from .configuration import Env as ConfigurationEnv
from .configuration import eval_module


@dataclass(frozen=False, eq=False)
class Service:
	name: str
	manager: genvm_tool.tests.exec.service.Service
	depends_on: list['Service'] | None = None
	meta: dict[str, typing.Any] = field(default_factory=dict)

	def __hash__(self):
		return id(self)

	def __eq__(self, other):
		return self is other


class Context:
	shared: SharedContext
	configuration: ConfigurationEnv
	_all_services: list[Service]
	_all_cases: list[genvm_tool.tests.test.Case]

	def new_service(
		self,
		name: str,
		manager: genvm_tool.tests.exec.service.Service,
		depends_on: list['Service'] | None = None,
	) -> Service:
		svc = Service(name=name, manager=manager, depends_on=depends_on)
		return svc

	def add_case(self, case: genvm_tool.tests.test.Case):
		assert isinstance(case, genvm_tool.tests.test.Case)
		self._all_cases.append(case)

	def collect_dir(self, relative: str, **kwargs: typing.Any) -> None:
		"""Evaluate ``<relative>/test.py`` and invoke its ``collect(ctx, **kwargs)``.

		This is how a test suite registers its cases: the ``test.py`` next to the
		tests owns the discovery + case-building, keeping it out of the importable
		plugins package.
		"""
		file = self.shared.root_dir / relative / const.TEST_FILE_NAME
		module = eval_module(file, self.shared.root_dir)
		module.collect(self, **kwargs)


class Env(NamedTuple):
	cases: list[genvm_tool.tests.test.Case]
	args: argparse.Namespace


def run(shared: SharedContext, configuration: ConfigurationEnv) -> Env:
	ctx = Context()
	ctx.shared = shared
	ctx.configuration = configuration
	ctx._all_services = []
	ctx._all_cases = []

	for collector in configuration.collectors:
		collector(ctx)

	return Env(
		cases=ctx._all_cases,
		args=configuration.args,
	)
