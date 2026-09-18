"""A service that never comes up must not wedge the run."""

import asyncio

import pytest
from genvm_tool.tests.exec import service


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
