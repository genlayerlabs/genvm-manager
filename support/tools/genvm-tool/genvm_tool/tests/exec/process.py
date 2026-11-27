import asyncio


async def stop(
	process: asyncio.subprocess.Process,
	*,
	terminate_timeout: float = 5.0,
	kill_timeout: float = 5.0,
) -> None:
	"""
	End a process without ever waiting on it indefinitely.

	A reap that never lands leaves a zombie, which is the lesser evil: the
	process has been killed either way, and a teardown that cannot finish wedges
	the whole run.
	"""
	try:
		process.terminate()
	except ProcessLookupError:
		return
	try:
		await asyncio.wait_for(process.wait(), timeout=terminate_timeout)
		return
	except asyncio.TimeoutError:
		pass
	process.kill()
	try:
		await asyncio.wait_for(process.wait(), timeout=kill_timeout)
	except asyncio.TimeoutError:
		pass


async def drain(tasks: list[asyncio.Task], *, timeout: float = 5.0) -> None:
	"""
	End tasks reading a process' pipes, without waiting for EOF.

	A reader only sees EOF once every process holding the write end is gone, and
	children the process spawned inherit it, so EOF may never come. Whatever is
	still unread when the timeout expires is dropped.
	"""
	_, pending = await asyncio.wait(tasks, timeout=timeout)
	for task in pending:
		task.cancel()
	# every task, not just the cancelled ones: an exception nobody retrieves
	# resurfaces as a warning at collection time
	await asyncio.gather(*tasks, return_exceptions=True)
