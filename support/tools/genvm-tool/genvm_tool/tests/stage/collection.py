from __future__ import annotations

import argparse
import typing
from dataclasses import dataclass, field
from typing import NamedTuple

import genvm_tool.tests
from genvm_tool.tests import SharedContext, const, tags

from .configuration import Env as ConfigurationEnv
from .configuration import eval_module


@dataclass(frozen=False, eq=False)
class Semaphore:
	name: str

	def __hash__(self):
		return id(self)

	def __eq__(self, other):
		return self is other


@dataclass(frozen=False, eq=False)
class Service:
	name: str
	manager: genvm_tool.tests.exec.service.Service
	depends_on: list['Service'] | None = None
	semaphores: frozenset[Semaphore] = frozenset()
	meta: dict[str, typing.Any] = field(default_factory=dict)
	handle: genvm_tool.tests.exec.service.Handle | None = None
	order: int = 0

	def __hash__(self):
		return id(self)

	def __eq__(self, other):
		return self is other


class Context:
	shared: SharedContext
	configuration: ConfigurationEnv
	_all_services: list[Service]
	_all_semaphores: list[Semaphore]
	_all_cases: list[genvm_tool.tests.test.Case]
	_tag_offenders: dict[str, set[str]]
	_tag_declaration_errors: set[tuple[str, str]]

	def new_semaphore(self, name: str) -> Semaphore:
		semaphore = Semaphore(name=name)
		self._all_semaphores.append(semaphore)
		return semaphore

	def new_service(
		self,
		name: str,
		manager: genvm_tool.tests.exec.service.Service,
		depends_on: list['Service'] | None = None,
		semaphores: typing.Iterable[Semaphore] = (),
	) -> Service:
		svc = Service(
			name=name,
			manager=manager,
			depends_on=depends_on,
			semaphores=frozenset(semaphores),
			order=len(self._all_services),
		)
		self._all_services.append(svc)
		return svc

	def add_case(self, case: genvm_tool.tests.test.Case):
		assert isinstance(case, genvm_tool.tests.test.Case)
		self._all_cases.append(case)

	def add_tag_offender(self, name: str, invalid: frozenset[str]) -> None:
		self._tag_offenders.setdefault(name, set()).update(invalid)

	def add_tag_declaration_error(self, name: str, error: str) -> None:
		self._tag_declaration_errors.add((name, error))

	def collect_dir(self, relative: str, **kwargs: typing.Any) -> None:
		"""
		Evaluate ``<relative>/test.py`` and invoke its ``collect(ctx, **kwargs)``.

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
	ctx._all_semaphores = []
	ctx._all_cases = []
	ctx._tag_offenders = {}
	ctx._tag_declaration_errors = set()

	for collector in configuration.collectors:
		collector(ctx)

	registry = shared.tags_registry
	for case in ctx._all_cases:
		expanded = tags.expand(case.description.tags, registry=registry)
		case.description = case.description._replace(tags=expanded)
		if registry is not None and (invalid := tags.unknown(registry, expanded)):
			ctx.add_tag_offender(case.description.name, invalid)

	if ctx._tag_offenders or ctx._tag_declaration_errors:
		errors = [
			(name, f'unknown test tags: {", ".join(sorted(invalid))}')
			for name, invalid in ctx._tag_offenders.items()
		]
		errors.extend(ctx._tag_declaration_errors)
		errors.sort()
		shown = errors[:20]
		lines = [f'{name}: {error}' for name, error in shown]
		if remaining := len(errors) - len(shown):
			lines.append(f'... and {remaining} more errors')
		registry_path = shared.config.get('tags_registry')
		registry_detail = (
			f'; valid tags are loaded from {registry_path}'
			if registry_path is not None
			else ''
		)
		raise ValueError(
			f'Invalid test tag declarations{registry_detail}:\n' + '\n'.join(lines)
		)

	return Env(
		cases=ctx._all_cases,
		args=configuration.args,
	)
