import subprocess
import sys
import threading
import typing
from pathlib import Path

import cloudpickle


class Watchdog:
	"""Owns the lifecycle of registered cleanups (e.g. ``docker stop``).

	Cleanups are picklable zero-argument callables shipped to a watchdog
	subprocess. The subprocess is the single executor: it runs a cleanup when
	asked (:meth:`kill`), runs every still-registered cleanup when this process
	exits normally (:meth:`stop`), and runs them too if this process dies
	unexpectedly (detected via reparenting). That way a container is always torn
	down through the watchdog and nothing is left dangling at the end of a run.
	"""

	def __init__(self, process: subprocess.Popen):
		self._process = process
		self._lock = threading.Lock()
		self._next_token = 0

	@staticmethod
	def start() -> 'Watchdog':
		script = Path(__file__).resolve().parent / 'watchdog_main.py'
		process = subprocess.Popen(
			[sys.executable, str(script)],
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL,
		)
		return Watchdog(process)

	def _send(self, msg: dict) -> None:
		assert self._process.stdin is not None
		payload = cloudpickle.dumps(msg)
		self._process.stdin.write(len(payload).to_bytes(4, 'big') + payload)
		self._process.stdin.flush()

	def register(self, cleanup: typing.Callable[[], typing.Any]) -> int:
		"""Register a cleanup callable; return a token to :meth:`kill` it later."""
		with self._lock:
			token = self._next_token
			self._next_token += 1
			try:
				self._send({'action': 'add', 'token': token, 'cleanup': cleanup})
			except (BrokenPipeError, OSError):
				pass
			return token

	def unregister(self, token: int) -> None:
		"""Forget a cleanup without running it."""
		with self._lock:
			try:
				self._send({'action': 'remove', 'token': token})
			except (BrokenPipeError, OSError):
				pass

	def kill(self, token: int) -> None:
		"""Run a registered cleanup now (via the watchdog) and forget it.

		Blocks until the watchdog acknowledges completion, so callers may rely on
		the resource (e.g. a container's port) being released on return. Safe to
		call from an event loop via ``asyncio.to_thread``.
		"""
		with self._lock:
			if self._process.poll() is not None:
				return
			try:
				self._send({'action': 'kill', 'token': token})
			except (BrokenPipeError, OSError):
				return
			assert self._process.stdout is not None
			try:
				# One ack line per kill; requests are serialized by the lock.
				self._process.stdout.readline()
			except (OSError, ValueError):
				pass

	def stop(self) -> None:
		"""Stop the watchdog, running any cleanups still registered.

		Closing stdin signals a normal exit: the subprocess tears down every
		dangling cleanup before exiting, so we wait for it to finish.
		"""
		if self._process.stdin:
			try:
				self._process.stdin.close()
			except OSError:
				pass
		try:
			self._process.wait(timeout=60)
		except subprocess.TimeoutExpired:
			self._process.kill()
			self._process.wait()
