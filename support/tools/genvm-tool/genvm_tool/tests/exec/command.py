import asyncio
import collections.abc
import contextlib
import enum
import functools
import io
import os
import re
import shlex
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from genvm_tool.tests import SharedContext

_ANSI_ESCAPE_RE = re.compile(rb'\x1b\[[0-9;]*[A-Za-z]')

# A command gets its own session, so a signal aimed at ours never reaches it: it
# has to be killed by pid, and it outlives us if we die before doing that
_KILL_GRACE = 5.0


def _kill(pid: int, *, group: bool) -> None:
	"""
	Kill ``pid``, politely first.

	Runs in the watchdog, which is not the parent and can reap nothing, so the
	exit is waited for by looking rather than by `wait`.
	"""
	send = os.killpg if group else os.kill
	for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
		try:
			send(pid, sig)
		except OSError:
			return
		deadline = time.monotonic() + _KILL_GRACE
		while time.monotonic() < deadline:
			time.sleep(0.1)
			try:
				send(pid, 0)
			except OSError:
				return


def kill_session_cleanup(pid: int) -> functools.partial:
	"""
	The picklable cleanup the watchdog runs for a command left behind.

	The whole group goes, not the session leader alone: what leaks is the fleet
	the command spawned.
	"""
	return functools.partial(_kill, pid, group=True)


def kill_process_cleanup(pid: int) -> functools.partial:
	"""Same, for a process sharing our own group, which must survive this."""
	return functools.partial(_kill, pid, group=False)


@dataclass
class Result:
	exit_code: int
	stdout: str
	stderr: str
	elapsed_seconds: float


async def _connect_reader(fd):
	loop = asyncio.get_event_loop()
	reader = asyncio.StreamReader(loop=loop)
	reader_proto = asyncio.StreamReaderProtocol(reader)
	transport, _ = await loop.connect_read_pipe(lambda: reader_proto, os.fdopen(fd, 'rb'))
	return reader, transport


async def _read_whole(reader, transport, *, duplicate_to: io.BytesIO | None) -> bytes:
	put_to = bytearray()
	try:
		while True:
			read = await reader.read(4096)
			if read is None or len(read) == 0:
				return put_to
			if duplicate_to is not None:
				duplicate_to.write(read)
				if b'\n' in read:
					duplicate_to.flush()
			put_to.extend(read)
	finally:
		try:
			transport.close()
		except OSError:
			pass
		await asyncio.sleep(0)


class RunMode(enum.Enum):
	SILENT = 'silent'
	INTERACTIVE = 'interactive'
	INTERACTIVE_TTY = 'interactive-tty'


@dataclass
class Command:
	args: collections.abc.Sequence[str | Path]
	cwd: Path
	env: dict[str, str]

	def to_script(self) -> list[str]:
		ret: list[str] = []

		ret.append('#!/bin/sh')

		ret.append('cd ' + shlex.quote(str(self.cwd)))
		for k, v in self.env.items():
			ret.append(f'export {k}={shlex.quote(v)}')
		ret.append(' '.join(map(lambda x: shlex.quote(str(x)), self.args)))

		return ret

	async def run(self, ctx: SharedContext, *, mode: RunMode) -> Result:
		import uuid

		uid = str(uuid.uuid4())
		ctx.logger.debug(
			'running command',
			env=self.env,
			args=self.args,
			cwd=self.cwd,
			id=uid,
		)

		if mode != RunMode.SILENT:
			ctx.printer.put(
				'running command',
				script=self.to_script(),
			)

		start = time.monotonic()

		if mode == RunMode.INTERACTIVE_TTY:
			stdout_writer = sys.stdout.fileno()
			stderr_writer = sys.stderr.fileno()
			stdin = None
			stdout_fut = asyncio.get_event_loop().create_future()
			stderr_fut = asyncio.get_event_loop().create_future()
			stdout_fut.set_result(b'')
			stderr_fut.set_result(b'')
		else:
			stdout_rfd, stdout_writer = os.pipe()
			stderr_rfd, stderr_writer = os.pipe()

			if mode == RunMode.INTERACTIVE:
				stdin = None
				stdout_dup = os.fdopen(sys.stdout.fileno(), 'wb', closefd=False)
				stderr_dup = os.fdopen(sys.stderr.fileno(), 'wb', closefd=False)
			elif mode == RunMode.SILENT:
				stdin = asyncio.subprocess.DEVNULL
				stdout_dup = None
				stderr_dup = None
			else:
				raise ValueError(f'Unknown run mode: {mode!r}')

			stdout_reader, stdout_transport = await _connect_reader(stdout_rfd)
			stderr_reader, stderr_transport = await _connect_reader(stderr_rfd)

			stdout_fut = asyncio.ensure_future(
				_read_whole(stdout_reader, stdout_transport, duplicate_to=stdout_dup)
			)
			stderr_fut = asyncio.ensure_future(
				_read_whole(stderr_reader, stderr_transport, duplicate_to=stderr_dup)
			)

		process = await asyncio.subprocess.create_subprocess_exec(
			*self.args,
			cwd=self.cwd,
			env=self.env,
			stdout=stdout_writer,
			stderr=stderr_writer,
			stdin=stdin,
			start_new_session=True,
		)

		if stdout_writer != sys.stdout.fileno():
			os.close(stdout_writer)
		if stderr_writer != sys.stderr.fileno():
			os.close(stderr_writer)

		ctx.logger.debug('process started', pid=process.pid, id=uid)
		# Registered for as long as it runs: a hard death of the tool leaves the
		# watchdog to kill it, and a cancellation is handled right here
		token = ctx.watchdog.register(kill_session_cleanup(process.pid))
		try:
			res = await process.wait()
		except asyncio.CancelledError:
			# Ask it to stop and leave the cleanup registered: whatever survives
			# this is killed when the watchdog is stopped, or when it notices we
			# are gone
			with contextlib.suppress(OSError):
				os.killpg(process.pid, signal.SIGINT)
			raise
		ctx.watchdog.unregister(token)
		end = time.monotonic()
		ctx.logger.debug('process ended', pid=process.pid, exit_code=res, id=uid)

		stdout_text = _ANSI_ESCAPE_RE.sub(b'', await stdout_fut).decode('utf-8')
		stderr_text = _ANSI_ESCAPE_RE.sub(b'', await stderr_fut).decode('utf-8')

		ctx.logger.trace(
			'process output',
			pid=process.pid,
			exit_code=res,
			stdout=stdout_text,
			stderr=stderr_text,
			id=uid,
		)

		return Result(
			exit_code=res,
			stdout=stdout_text,
			stderr=stderr_text,
			elapsed_seconds=end - start,
		)
