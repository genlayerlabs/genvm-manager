from __future__ import annotations

import typing
from types import SimpleNamespace

import genvm_tool.tests
from genvm_tool.tests import SharedContext

from .collection import Env as CollectionEnv
from .collection import Service


class StartCases(typing.NamedTuple):
	id: int
	cases: list[genvm_tool.tests.test.Case]


class AwaitAllCases(typing.NamedTuple):
	id: int


class StartService(typing.NamedTuple):
	service: Service


class StopService(typing.NamedTuple):
	service: Service


type Action = StartCases | AwaitAllCases | StartService | StopService


class Env(typing.NamedTuple):
	actions: list[Action]
	args: SimpleNamespace


def _service_closure(service: Service) -> set[Service]:
	result: set[Service] = set()
	pending = [service]
	while pending:
		current = pending.pop()
		if current in result:
			continue
		result.add(current)
		pending.extend(current.depends_on or [])
	return result


def _topo_sort_services(
	services: set[Service],
	precedence: set[tuple[Service, Service]],
) -> list[Service]:
	"""
	The order the services are started in.

	Dependencies come before dependents, and the owners of one semaphore come one
	after another, with the whole dependent subtree of the previous owner started
	before the next one takes it. Which owner goes first is a free choice, and
	two semaphores whose owners depend on each other can only be ordered jointly,
	so the choices are searched rather than fixed one semaphore at a time. Among
	the services that may go next the one declared first wins, so an
	unconstrained suite keeps its declaration order.
	"""
	closures = {service: _service_closure(service) & services for service in services}
	predecessors: dict[Service, set[Service]] = {
		service: {
			dependency for dependency in service.depends_on or [] if dependency in services
		}
		for service in services
	}
	for before, after in precedence:
		if before in services and after in services:
			predecessors[after].add(before)

	def can_start(
		candidate: Service,
		started: frozenset[Service],
		remaining: tuple[Service, ...],
	) -> bool:
		if not predecessors[candidate].issubset(started):
			return False
		# Taking a semaphore stops whoever owns it and everything depending on
		# that owner, so all of it has to have been started already
		return not any(
			holder in started and holder.semaphores & candidate.semaphores
			for other in remaining
			if other is not candidate
			for holder in closures[other]
		)

	# One dead end is enough to rule out every way of reaching the same set of
	# started services, which keeps the search out of the combinatorial case
	dead_ends: set[frozenset[Service]] = set()

	def search(
		started: frozenset[Service],
		remaining: tuple[Service, ...],
	) -> list[Service] | None:
		if not remaining:
			return []
		if started in dead_ends:
			return None
		for candidate in remaining:
			if not can_start(candidate, started, remaining):
				continue
			tail = search(
				started | {candidate},
				tuple(item for item in remaining if item is not candidate),
			)
			if tail is not None:
				return [candidate, *tail]
		dead_ends.add(started)
		return None

	result = search(
		frozenset(),
		tuple(sorted(services, key=lambda service: service.order)),
	)
	if result is None:
		names = ', '.join(sorted(service.name for service in services))
		raise ValueError(
			'services cannot be started in any order, their dependencies and '
			f'semaphores contradict each other: {names}'
		)
	return result


def _validate_test_dependencies(
	cases: list[genvm_tool.tests.test.Case],
) -> dict[str, genvm_tool.tests.test.Case]:
	"""
	Validate test-to-test dependencies: check for circular deps and return cases_by_name.
	Dependencies on names not in the current set are silently ignored (e.g. filtered out).
	"""
	cases_by_name: dict[str, genvm_tool.tests.test.Case] = {}
	for case in cases:
		cases_by_name[case.description.name] = case

	# Detect circular dependencies via DFS
	visited: set[str] = set()
	in_stack: set[str] = set()

	def visit(name: str) -> None:
		if name in visited:
			return
		if name in in_stack:
			raise ValueError(f'Circular test dependency detected involving {name}')
		in_stack.add(name)
		case = cases_by_name.get(name)
		if case is not None:
			for dep_name in case.description.depends_on:
				if dep_name in cases_by_name:
					visit(dep_name)
		in_stack.remove(name)
		visited.add(name)

	for name in cases_by_name:
		visit(name)

	return cases_by_name


def _case_services(case: genvm_tool.tests.test.Case) -> set[Service]:
	services: set[Service] = set()
	for svc in case.description.needed_services:
		services.update(_service_closure(svc))
	return services


def _test_dependency_names(
	case: genvm_tool.tests.test.Case,
	cases_by_name: dict[str, genvm_tool.tests.test.Case],
	memo: dict[str, frozenset[str]],
) -> frozenset[str]:
	if case.description.name in memo:
		return memo[case.description.name]

	result: set[str] = set()
	for name in case.description.depends_on:
		dependency = cases_by_name.get(name)
		if dependency is None:
			continue
		result.add(name)
		result.update(_test_dependency_names(dependency, cases_by_name, memo))
	memo[case.description.name] = frozenset(result)
	return memo[case.description.name]


def run(shared: SharedContext, collection_env: CollectionEnv) -> Env:
	next_id = 1
	actions: list[Action] = []

	# Track running batches: batch_id -> set of service names the batch depends on
	running_batches: dict[int, set[str]] = {}

	# Validate test dependencies and build name lookup
	cases_by_name = _validate_test_dependencies(collection_env.cases)

	# Collect all services needed by the selected cases
	all_needed_services: set[Service] = set()
	for case in collection_env.cases:
		all_needed_services.update(_case_services(case))

	# A dependency between tests using different holders of one semaphore orders
	# those service lifetimes. The dependency runs first, then its holder can be
	# stopped and the dependent's holder started
	service_precedence: set[tuple[Service, Service]] = set()
	test_dependencies_memo: dict[str, frozenset[str]] = {}
	for case in collection_env.cases:
		for dep_name in _test_dependency_names(
			case,
			cases_by_name,
			test_dependencies_memo,
		):
			dep_case = cases_by_name.get(dep_name)
			if dep_case is None:
				continue
			for before in _case_services(dep_case):
				for after in _case_services(case):
					if before is not after and before.semaphores & after.semaphores:
						service_precedence.add((before, after))

	# Topo sort: dependencies first, dependents last
	service_names = [service.name for service in all_needed_services]
	if len(service_names) != len(set(service_names)):
		duplicates = sorted(
			{name for name in service_names if service_names.count(name) > 1}
		)
		raise ValueError(f'duplicate service names: {", ".join(duplicates)}')
	topo_sorted_services = _topo_sort_services(
		all_needed_services,
		service_precedence,
	)
	services_by_name = {service.name: service for service in topo_sorted_services}

	def get_required_service_names(case: genvm_tool.tests.test.Case) -> frozenset[str]:
		return frozenset(service.name for service in _case_services(case))

	for case in collection_env.cases:
		services = [services_by_name[name] for name in get_required_service_names(case)]
		for i, left in enumerate(services):
			for right in services[i + 1 :]:
				shared_semaphores = left.semaphores & right.semaphores
				if shared_semaphores:
					names = ', '.join(sorted(item.name for item in shared_semaphores))
					raise ValueError(
						f'{case.description.name} requires mutually exclusive services '
						f'{left.name} and {right.name} (semaphores: {names})'
					)

	# Start services in topo order and cases as soon as their services are ready
	active_services: set[str] = set()
	remaining_cases = list(collection_env.cases)
	scheduled_cases: set[str] = set()

	def await_batches_for(service_names: set[str]) -> None:
		for batch_id, batch_services in list(running_batches.items()):
			if batch_services & service_names:
				actions.append(AwaitAllCases(id=batch_id))
				del running_batches[batch_id]

	def stop_services(service_names: set[str]) -> None:
		await_batches_for(service_names)
		for service in reversed(topo_sorted_services):
			if service.name in service_names and service.name in active_services:
				actions.append(StopService(service=service))
				active_services.remove(service.name)

	def services_ready(case: genvm_tool.tests.test.Case) -> bool:
		return get_required_service_names(case).issubset(active_services)

	def dependencies_ready(
		case: genvm_tool.tests.test.Case,
		also_starting: frozenset[str],
	) -> bool:
		return _test_dependency_names(
			case,
			cases_by_name,
			test_dependencies_memo,
		).issubset(scheduled_cases | also_starting)

	def schedule_ready_cases() -> None:
		nonlocal next_id, remaining_cases

		while True:
			# A batch is awaited as a whole, so a case waiting for a dependency
			# that only starts after that await would block the runner forever.
			# Cases of one batch run concurrently, hence may depend on each other
			parallel_batch = [
				case
				for case in remaining_cases
				if not case.description.console_pool and services_ready(case)
			]
			while True:
				starting = frozenset(case.description.name for case in parallel_batch)
				kept = [case for case in parallel_batch if dependencies_ready(case, starting)]
				if len(kept) == len(parallel_batch):
					break
				parallel_batch = kept

			if parallel_batch:
				batch_id = next_id
				next_id += 1
				batch_services: set[str] = set()
				for case in parallel_batch:
					batch_services.update(get_required_service_names(case))
					scheduled_cases.add(case.description.name)
				started = {case.description.name for case in parallel_batch}
				remaining_cases = [
					case for case in remaining_cases if case.description.name not in started
				]
				actions.append(StartCases(id=batch_id, cases=parallel_batch))
				running_batches[batch_id] = batch_services

			# A console case holds the whole case pool, so it runs alone. Start it
			# only once every case it depends on has started, otherwise it would
			# wait for a later service that cannot start while it holds the pool
			ready_console = next(
				(
					case
					for case in remaining_cases
					if case.description.console_pool
					and services_ready(case)
					and dependencies_ready(case, frozenset())
				),
				None,
			)
			if ready_console is not None:
				remaining_cases.remove(ready_console)
				scheduled_cases.add(ready_console.description.name)
				actions.append(StartCases(id=next_id, cases=[ready_console]))
				actions.append(AwaitAllCases(id=next_id))
				next_id += 1

			# A console case that finished may unblock cases that depend on it
			if not parallel_batch and ready_console is None:
				return

	# Cases without services can start before the first service
	schedule_ready_cases()

	for svc in topo_sorted_services:
		conflicting = {
			name
			for name in active_services
			if services_by_name[name].semaphores & svc.semaphores
		}
		if conflicting:
			# A dependent cannot outlive a service being displaced by a mutually
			# exclusive alternative
			while True:
				dependents = {
					name
					for name in active_services
					if any(
						dep.name in conflicting for dep in services_by_name[name].depends_on or []
					)
				}
				if dependents.issubset(conflicting):
					break
				conflicting.update(dependents)
			stop_services(conflicting)

		missing_dependencies = {
			dep.name for dep in svc.depends_on or [] if dep.name not in active_services
		}
		if missing_dependencies:
			raise ValueError(
				f'{svc.name} cannot start after mutually exclusive dependencies stopped: '
				+ ', '.join(sorted(missing_dependencies))
			)

		# Start this service
		actions.append(StartService(service=svc))
		active_services.add(svc.name)

		schedule_ready_cases()

	if remaining_cases:
		names = ', '.join(case.description.name for case in remaining_cases)
		raise ValueError(
			f'cases could not be scheduled before their services stopped: {names}'
		)

	# Ending: stop services in reverse topo order (dependents first, dependencies last)
	stop_services(set(active_services))

	# Await any remaining batches (e.g., those without service dependencies)
	for batch_id in list(running_batches.keys()):
		actions.append(AwaitAllCases(id=batch_id))
	running_batches.clear()

	return Env(
		actions=actions,
		args=collection_env.args,
	)
