import abc
import asyncio
import typing


class Handle(metaclass=abc.ABCMeta):
	# Budgets for a plain service; a subclass whose probe is slower to answer
	# (see :class:`ContainerHandle`) raises them.
	STARTUP_TIMEOUT = 60.0
	INTERRUPT_TIMEOUT = 30.0

	@abc.abstractmethod
	async def healthy(self) -> bool: ...

	@abc.abstractmethod
	async def interrupt(self) -> None: ...

	async def death_reason(self) -> str | None:
		"""
		Why the service can no longer become healthy, or None while it still might.
		"""
		return None

	async def shutdown(self) -> None:
		"""
		:meth:`interrupt` that cannot outlive its budget.
		"""
		try:
			await asyncio.wait_for(self.interrupt(), timeout=self.INTERRUPT_TIMEOUT)
		except asyncio.TimeoutError:
			raise RuntimeError(
				f'Service did not stop within {self.INTERRUPT_TIMEOUT:g}s'
			) from None

	async def await_startup(self) -> typing.Self:
		# the deadline covers the probes themselves, not just the gaps between them
		try:
			async with asyncio.timeout(self.STARTUP_TIMEOUT):
				while True:
					reason = await self.death_reason()
					if reason is not None:
						break
					if await self.healthy():
						return self
					await asyncio.sleep(1)
		except TimeoutError:
			reason = f'it was not healthy within {self.STARTUP_TIMEOUT:g}s'
		teardown = await self._shutdown_after_failure()
		if teardown is not None:
			reason = f'{reason}; teardown failed too: {teardown}'
		raise RuntimeError(f'Service failed to start: {reason}')

	async def _shutdown_after_failure(self) -> str | None:
		"""
		Tear down a service that never started; reports why teardown itself failed.
		"""
		# reported rather than raised: a container left behind must not hide why
		# the service never came up
		try:
			await self.shutdown()
		except Exception as e:
			return str(e)
		return None


class Service(metaclass=abc.ABCMeta):
	@abc.abstractmethod
	async def start(self) -> Handle: ...


class FunctionService(Service):
	def __init__(self, start_func: typing.Callable[[], typing.Awaitable[Handle]]):
		self._start_func = start_func

	async def start(self) -> Handle:
		return await self._start_func()
