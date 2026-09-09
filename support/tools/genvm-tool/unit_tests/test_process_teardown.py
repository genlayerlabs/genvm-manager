"""Ending a service must not depend on a reap landing or a pipe reaching EOF."""

import asyncio

from genvm_tool.tests.exec import process


class _Process:
	"""A process that ignores every signal and is never reaped."""

	returncode = None

	def __init__(self):
		self.terminated = False
		self.killed = False

	def terminate(self):
		self.terminated = True

	def kill(self):
		self.killed = True

	async def wait(self):
		await asyncio.Event().wait()


class _ExitingProcess(_Process):
	async def wait(self):
		self.returncode = 0
		return 0


def test_a_process_that_exits_is_not_killed():
	proc = _ExitingProcess()
	asyncio.run(process.stop(proc))
	assert proc.terminated
	assert not proc.killed


def test_an_unreapable_process_does_not_wedge_teardown():
	proc = _Process()

	async def go():
		await asyncio.wait_for(
			process.stop(proc, terminate_timeout=0.1, kill_timeout=0.1), timeout=5
		)

	asyncio.run(go())
	assert proc.killed


def test_a_vanished_process_is_not_an_error():
	class _Gone(_Process):
		def terminate(self):
			raise ProcessLookupError

	asyncio.run(process.stop(_Gone()))


def test_readers_that_never_see_eof_are_cancelled():
	async def go():
		tasks = [asyncio.create_task(asyncio.Event().wait()) for _ in range(2)]
		await asyncio.wait_for(process.drain(tasks, timeout=0.1), timeout=5)
		return tasks

	tasks = asyncio.run(go())
	assert all(task.cancelled() for task in tasks)


def test_a_reader_that_finished_is_left_alone():
	async def go():
		tasks = [asyncio.create_task(asyncio.sleep(0))]
		await process.drain(tasks, timeout=5)
		return tasks

	tasks = asyncio.run(go())
	assert all(task.done() and not task.cancelled() for task in tasks)


def test_a_reader_that_raised_does_not_warn():
	async def boom():
		raise RuntimeError('log write failed')

	async def go():
		tasks = [asyncio.create_task(boom())]
		await process.drain(tasks, timeout=5)
		# retrieved by drain, so nothing resurfaces at collection time
		assert tasks[0].exception() is not None

	asyncio.run(go())
