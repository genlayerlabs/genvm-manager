#!/usr/bin/env python3

import sys
from argparse import Namespace
from collections.abc import Iterable
from dataclasses import dataclass

import genvm_tool.tests
from genvm_tool.tests.stage import scheduling
from genvm_tool.tests.stage.collection import Env, Semaphore, Service


MAX_SERVICES = 8
MAX_SEMAPHORES = 4
MAX_CASES = 12


class _Service(genvm_tool.tests.exec.service.Service):
	async def start(self):
		raise AssertionError('fuzz scheduling must not start services')


@dataclass
class Reader:
	data: bytes
	offset: int = 0

	def byte(self) -> int:
		if self.offset >= len(self.data):
			return 0
		result = self.data[self.offset]
		self.offset += 1
		return result

	def mask(self, width: int) -> int:
		result = 0
		for shift in range(0, width, 8):
			result |= self.byte() << shift
		return result


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


def _closure_of(services: Iterable[Service]) -> set[Service]:
	result: set[Service] = set()
	for service in services:
		result.update(_service_closure(service))
	return result


def _case_services(case: genvm_tool.tests.test.Case) -> set[Service]:
	return _closure_of(case.description.needed_services)


def _conflict_free(services: set[Service]) -> bool:
	claimed: set[Semaphore] = set()
	for service in services:
		if claimed & service.semaphores:
			return False
		claimed.update(service.semaphores)
	return True


def _make_services(reader: Reader) -> list[Service]:
	semaphore_count = reader.byte() % (MAX_SEMAPHORES + 1)
	semaphores = [Semaphore(f'mutex-{index}') for index in range(semaphore_count)]
	services: list[Service] = []

	for index in range(reader.byte() % (MAX_SERVICES + 1)):
		semaphore_mask = reader.mask(semaphore_count)
		service = Service(
			name=f'service-{index}',
			manager=_Service(),
			semaphores=frozenset(
				semaphore
				for bit, semaphore in enumerate(semaphores)
				if semaphore_mask & (1 << bit)
			),
			order=index,
			depends_on=[],
		)
		# A service whose closure holds two owners of one semaphore could never be
		# up, since starting the second one stops the first
		dependency_mask = reader.mask(index)
		closure = {service}
		for bit, dependency in enumerate(services):
			if not dependency_mask & (1 << bit):
				continue
			candidate = closure | _service_closure(dependency)
			if _conflict_free(candidate):
				service.depends_on.append(dependency)
				closure = candidate
		services.append(service)

	return services


def _cover_case(service: Service, index: int) -> genvm_tool.tests.test.Case:
	return genvm_tool.tests.test.StepsCase(
		description=genvm_tool.tests.test.Description(
			name=f'cover-{index}',
			needed_services=frozenset({service}),
		),
		steps=[],
	)


def _service_timeline(services: list[Service]) -> list[Service]:
	"""
	The order the scheduler starts ``services`` in, when every one of them is
	needed and no test dependency constrains their lifetimes.

	Taken from a probe schedule rather than recomputed here: the timeline is the
	scheduler's own policy, and a harness reimplementing it would only be
	checking its copy against the original. What the invariants below check is
	the meaning of a schedule, not this order.
	"""
	probe = Env(
		cases=[_cover_case(service, index) for index, service in enumerate(services)],
		args=Namespace(),
	)
	return [
		action.service
		for action in scheduling.run(None, probe).actions
		if isinstance(action, scheduling.StartService)
	]


class _Timeline:
	"""Which services can be up together, given the order they are started in."""

	def __init__(self, services: list[Service]):
		self.order = _service_timeline(services)
		self.position = {service: index for index, service in enumerate(self.order)}

	def _survives(self, service: Service, until: int) -> bool:
		# Starting a service stops every owner of a semaphore it takes, and
		# whatever depends on those transitively
		closure = _service_closure(service)
		for other in self.order[self.position[service] + 1 : until + 1]:
			if any(
				member.semaphores & other.semaphores
				for member in closure
				if member is not other
			):
				return False
		return True

	def phase(self, services: set[Service]) -> int:
		"""When the last of ``services`` starts, or -1 for a case needing none."""
		return max((self.position[service] for service in services), default=-1)

	def can_be_up_together(self, services: set[Service]) -> bool:
		until = self.phase(services)
		return all(self._survives(service, until) for service in services)

	def can_precede(self, before: set[Service], after: set[Service]) -> bool:
		"""
		Whether a case on ``before`` may be a dependency of one on ``after``.

		Their shared semaphores are handed over in one direction only, and the
		dependency has to have run by the time the dependent's services are up
		"""
		if self.phase(before) > self.phase(after):
			return False
		return all(
			self.position[left] < self.position[right]
			for left in before
			for right in after
			if left is not right and left.semaphores & right.semaphores
		)


def _make_input(data: bytes) -> Env:
	reader = Reader(data)
	services = _make_services(reader)
	timeline = _Timeline(services)

	# Every service is covered, so the timeline the cases are built against is the
	# one the scheduler ends up using. A service no case needs is not an input the
	# scheduler ever sees: it reaches it through the cases alone
	cases = [_cover_case(service, index) for index, service in enumerate(services)]
	case_services: list[set[Service]] = [_case_services(case) for case in cases]
	case_dependencies: list[set[int]] = [set() for _ in cases]

	for index in range(reader.byte() % (MAX_CASES + 1)):
		service_mask = reader.mask(len(services))
		needed: set[Service] = set()
		for bit, service in enumerate(services):
			if not service_mask & (1 << bit):
				continue
			candidate = _closure_of(needed | {service})
			if timeline.can_be_up_together(candidate):
				needed.add(service)
		required = _closure_of(needed)

		dependency_mask = reader.mask(len(cases))
		dependencies: set[int] = set()
		for bit in range(len(cases)):
			if not dependency_mask & (1 << bit):
				continue
			candidate_dependencies = {bit} | case_dependencies[bit]
			if all(
				timeline.can_precede(case_services[dependency], required)
				for dependency in candidate_dependencies
			):
				dependencies.update(candidate_dependencies)

		case_services.append(required)
		case_dependencies.append(dependencies)
		cases.append(
			genvm_tool.tests.test.StepsCase(
				description=genvm_tool.tests.test.Description(
					name=f'case-{index}',
					needed_services=frozenset(needed),
					console_pool=bool(reader.byte() & 1),
					depends_on=frozenset(
						cases[dependency].description.name for dependency in dependencies
					),
				),
				steps=[],
			)
		)

	# Collection order is whatever the suites happened to register, and the
	# schedule must not depend on it
	for index in range(len(cases) - 1, 0, -1):
		other = reader.byte() % (index + 1)
		cases[index], cases[other] = cases[other], cases[index]

	return Env(cases=cases, args=Namespace())


def _test_dependencies(
	case: genvm_tool.tests.test.Case,
	cases_by_name: dict[str, genvm_tool.tests.test.Case],
) -> set[str]:
	result: set[str] = set()
	pending = list(case.description.depends_on)
	while pending:
		name = pending.pop()
		if name in result:
			continue
		result.add(name)
		pending.extend(cases_by_name[name].description.depends_on)
	return result


def _check_invariants(
	cases: list[genvm_tool.tests.test.Case],
	actions: list[scheduling.Action],
) -> None:
	cases_by_name = {case.description.name: case for case in cases}
	needed_services = {service for case in cases for service in _case_services(case)}
	active_services: set[Service] = set()
	started_services: set[Service] = set()
	stopped_services: set[Service] = set()
	started_cases: set[str] = set()
	running_batches: dict[int, list[genvm_tool.tests.test.Case]] = {}
	seen_batches: set[int] = set()

	for action in actions:
		if isinstance(action, scheduling.StartService):
			service = action.service
			assert service in needed_services
			assert service not in started_services
			assert set(service.depends_on or []).issubset(active_services)
			assert all(
				not service.semaphores & active.semaphores for active in active_services
			)
			started_services.add(service)
			active_services.add(service)
		elif isinstance(action, scheduling.StopService):
			service = action.service
			assert service in active_services
			assert all(
				service not in _service_closure(active) - {active} for active in active_services
			)
			assert all(
				service not in _case_services(case)
				for batch in running_batches.values()
				for case in batch
			)
			active_services.remove(service)
			stopped_services.add(service)
		elif isinstance(action, scheduling.StartCases):
			assert action.id not in seen_batches
			assert action.cases
			assert len({case.description.name for case in action.cases}) == len(action.cases)
			previously_started = set(started_cases)
			for case in action.cases:
				assert cases_by_name.get(case.description.name) is case
				assert case.description.name not in started_cases
				assert _case_services(case).issubset(active_services)
				if case.description.console_pool:
					# It holds the whole case pool, so nothing it waits for may
					# still be waiting for the runner to start it
					assert len(action.cases) == 1
					assert _test_dependencies(case, cases_by_name).issubset(previously_started)
				started_cases.add(case.description.name)
			seen_batches.add(action.id)
			running_batches[action.id] = action.cases
		elif isinstance(action, scheduling.AwaitAllCases):
			assert action.id in running_batches
			batch = running_batches.pop(action.id)
			for case in batch:
				# The runner blocks here, so a dependency that has not started by
				# now never will
				assert _test_dependencies(case, cases_by_name).issubset(started_cases)
		else:
			raise AssertionError(f'unknown scheduling action: {action!r}')

	assert started_cases == set(cases_by_name)
	assert started_services == needed_services
	assert stopped_services == needed_services
	assert not active_services
	assert not running_batches


def fuzz(data: bytes) -> None:
	collection_env = _make_input(data)
	schedule = scheduling.run(None, collection_env)
	_check_invariants(collection_env.cases, schedule.actions)


def main() -> None:
	import afl

	stream = sys.stdin.buffer
	while afl.loop(1000):
		stream.seek(0)
		fuzz(stream.read())


if __name__ == '__main__':
	main()
