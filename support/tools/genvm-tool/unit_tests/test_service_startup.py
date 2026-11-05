"""A service that never comes up must not wedge the run."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from genvm_tool.tests import SharedContext, test
from genvm_tool.tests.exec import service
from genvm_tool.tests.stage import collection, execution, scheduling


class _Handle(service.Handle):
	STARTUP_TIMEOUT = 0.3
	INTERRUPT_TIMEOUT = 0.2

	def __init__(self, *, healthy_after: int = 10**9, reason: str | None = None):
		self.probes = 0
		self.interrupted = False
		self._healthy_after = healthy_after
		self._reason = reason

	async def healthy(self) -> bool:
		self.probes += 1
		return self.probes >= self._healthy_after

	async def death_reason(self) -> str | None:
		return self._reason

	async def interrupt(self) -> None:
		self.interrupted = True


class _WedgedHandle(_Handle):
	async def interrupt(self) -> None:
		await asyncio.Event().wait()


def test_a_healthy_service_is_not_torn_down():
	handle = _Handle(healthy_after=1)
	assert asyncio.run(handle.await_startup()) is handle
	assert not handle.interrupted


def test_startup_gives_up_on_its_own_deadline():
	handle = _Handle()
	with pytest.raises(RuntimeError, match='not healthy within'):
		asyncio.run(handle.await_startup())
	assert handle.interrupted


def test_a_dead_service_is_not_waited_out():
	handle = _Handle(reason='exited with code 1')
	with pytest.raises(RuntimeError, match='exited with code 1'):
		asyncio.run(handle.await_startup())
	assert handle.probes == 0


def test_shutdown_outlives_a_hanging_interrupt():
	async def go():
		with pytest.raises(RuntimeError, match='did not stop'):
			await asyncio.wait_for(_WedgedHandle().shutdown(), timeout=5)

	asyncio.run(go())


def test_a_hanging_interrupt_does_not_mask_the_startup_failure():
	async def go():
		with pytest.raises(RuntimeError, match='not healthy within.*teardown failed too'):
			await asyncio.wait_for(_WedgedHandle().await_startup(), timeout=5)

	asyncio.run(go())


def test_a_wedged_probe_cannot_outlive_the_deadline():
	class _WedgedProbe(_Handle):
		async def healthy(self) -> bool:
			await asyncio.Event().wait()

	handle = _WedgedProbe()
	with pytest.raises(RuntimeError, match='not healthy within'):
		asyncio.run(asyncio.wait_for(handle.await_startup(), timeout=5))
	assert handle.interrupted


@pytest.mark.parametrize('failure', ['spawn', 'dead', 'timeout'])
def test_startup_failure_fails_dependents_and_continues(tmp_path, failure):
	handle = _Handle(reason='exited with code 1' if failure == 'dead' else None)
	start = AsyncMock(return_value=handle)
	if failure == 'spawn':
		start.side_effect = RuntimeError('could not spawn')
	failed = collection.Service('failed', service.FunctionService(start))
	dependent_start = AsyncMock()
	dependent = collection.Service(
		'dependent', service.FunctionService(dependent_start), depends_on=[failed]
	)
	healthy = _Handle(healthy_after=1)
	unrelated = collection.Service(
		'unrelated', service.FunctionService(AsyncMock(return_value=healthy))
	)
	cases = []
	for name, needed in [
		('direct-1', failed),
		('direct-2', failed),
		('transitive', dependent),
		('unrelated', unrelated),
	]:
		case = test.StepsCase(
			test.Description(name, needed_services=frozenset({needed})),
			[test.CONST_PASSED],
		)
		case.into_steps = AsyncMock(wraps=case.into_steps)
		cases.append(case)
	shared = SharedContext(tmp_path, logger=Mock(), printer=Mock(), watchdog=Mock())
	plan = scheduling.run(
		shared, collection.Env(cases=cases, args=SimpleNamespace(max_concurrent=2))
	)
	result = asyncio.run(asyncio.wait_for(execution.run(shared, plan), timeout=5))
	assert set(result.failed) == {'direct-1', 'direct-2', 'transitive'}
	assert result.success_count == 1
	assert len(result.results) == 4
	for record in result.results:
		if record.name != 'unrelated':
			assert not record.passed
			assert 'Service failed failed:' in record.failure_message
	for case in cases[:3]:
		case.into_steps.assert_not_called()
	dependent_start.assert_not_called()
	assert healthy.interrupted
	assert all(s.handle is None for s in [failed, dependent, unrelated])
