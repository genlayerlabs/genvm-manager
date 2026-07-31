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

	Cleanups are also kept here, and are run in-process whenever the watchdog is
	not available to run them (it stopped responding, or died before exiting
	cleanly). Otherwise a broken watchdog would silently leak every container.
	"""

	def __init__(self, process: subprocess.Popen):
		self._process = process
		self._lock = threading.Lock()
		self._next_token = 0
		self._cleanups: dict[int, typing.Callable[[], typing.Any]] = {}
		# Cleared as soon as the watchdog misses anything we asked of it: from
		# then on its exit status says nothing about what it actually ran.
		self._healthy = True

	@staticmethod
	def start() -> 'Watchdog':
		script = Path(__file__).resolve().parent / 'watchdog_main.py'
		process = subprocess.Popen(
			[sys.executable, str(script)],
			stdin=subprocess.PIPE,
			stdout=subprocess.PIPE,
			stderr=subprocess.DEVNULL,
			# Own session: Ctrl+C (and any other signal aimed at our process
			# group) must not reach the watchdog, it has cleanups to run.
			start_new_session=True,
		)
		return Watchdog(process)

	def _send(self, msg: dict) -> bool:
		"""Send one frame; ``False`` if the watchdog is gone."""
		if self._process.stdin is None or self._process.poll() is not None:
			self._healthy = False
			return False
		payload = cloudpickle.dumps(msg)
		try:
			self._process.stdin.write(len(payload).to_bytes(4, 'big') + payload)
			self._process.stdin.flush()
		except (OSError, ValueError):
			self._healthy = False
			return False
		return True

	def register(self, cleanup: typing.Callable[[], typing.Any]) -> int:
		"""Register a cleanup callable; return a token to :meth:`kill` it later."""
		with self._lock:
			token = self._next_token
			self._next_token += 1
			self._cleanups[token] = cleanup
			self._send({'action': 'add', 'token': token, 'cleanup': cleanup})
			return token

	def unregister(self, token: int) -> None:
		"""Forget a cleanup without running it."""
		with self._lock:
			self._cleanups.pop(token, None)
			self._send({'action': 'remove', 'token': token})

	def kill(self, token: int) -> None:
		"""Run a registered cleanup now (via the watchdog) and forget it.

		Blocks until the watchdog acknowledges completion, so callers may rely on
		the resource (e.g. a container's port) being released on return. Safe to
		call from an event loop via ``asyncio.to_thread``.
		"""
		with self._lock:
			cleanup = self._cleanups.pop(token, None)
			if self._send({'action': 'kill', 'token': token}) and self._await_ack(token):
				return
			# The watchdog never acknowledged the request: it is dead, or died
			# while running the cleanup. Run it here instead - doing it twice is
			# harmless, not doing it at all leaks the resource.
			self._healthy = False
			if cleanup is not None:
				_run_cleanup(cleanup)

	def _await_ack(self, token: int) -> bool:
		"""Wait for this kill's ack, skipping stale ones. ``False`` on EOF."""
		assert self._process.stdout is not None
		while True:
			try:
				line = self._process.stdout.readline()
			except (OSError, ValueError):
				return False
			if not line:
				return False
			try:
				if int(line) == token:
					return True
			except ValueError:
				return False

	def stop(self) -> None:
		"""Stop the watchdog, running any cleanups still registered.

		Closing stdin signals the subprocess to exit: it tears down every dangling
		cleanup first, so we wait for it to finish. It exits 0 only after they all
		ran, so any other status means we have to run them.

		Deliberately not under ``_lock``: a :meth:`kill` may be blocked waiting
		for an ack, and closing stdin is what unwedges it (the watchdog acks a
		pending kill before it ever sees the EOF).
		"""
		if self._process.stdin:
			try:
				self._process.stdin.close()
			except OSError:
				pass
		try:
			code = self._process.wait(timeout=60)
		except subprocess.TimeoutExpired:
			self._process.kill()
			code = self._process.wait()
		with self._lock:
			cleanups, self._cleanups = self._cleanups, {}
			healthy = self._healthy
			self._healthy = False
		if code != 0 or not healthy:
			for cleanup in cleanups.values():
				_run_cleanup(cleanup)


def _run_cleanup(cleanup: typing.Callable[[], typing.Any]) -> None:
	try:
		cleanup()
	except Exception:
		pass
