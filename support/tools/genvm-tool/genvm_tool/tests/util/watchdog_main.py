#!/usr/bin/env python3
"""
Watchdog process for genvm-tool test.

Started by the test runner as a child process, in its own session and with
terminating signals ignored, so that Ctrl+C does not take it down before it had
a chance to clean up. It owns the cleanups (e.g. ``docker stop``) registered by
the runner and is the single place that runs them, so a container is always torn
down through the watchdog:

- on a ``kill`` request: runs that one cleanup now and acknowledges it;
- when the runner is gone - stdin closed, or reparenting for the cases where no
	EOF ever comes - runs every still-registered cleanup, whether the runner
	exited normally or died on us.

Communication protocol (length-prefixed cloudpickle frames on stdin, a 4-byte
big-endian length followed by the pickled message dict):
- {"action": "add", "token": int, "cleanup": <callable>}
- {"action": "remove", "token": int}
- {"action": "kill", "token": int}
A kill is acknowledged with a single ``<token>\\n`` line on stdout.
"""

import os
import select
import signal
import sys

import cloudpickle


def _run_cleanup(cleanup) -> None:
	try:
		cleanup()
	except Exception:
		pass


def _read(fd: int, timeout: float) -> bytes | None:
	"""Pending stdin bytes, ``b''`` if none within ``timeout``, ``None`` on EOF."""
	try:
		ready, _, _ = select.select([fd], [], [], timeout)
		if not ready:
			return b''
		return os.read(fd, 65536) or None
	except (ValueError, OSError):
		return None


def _consume(buffer: bytes, cleanups: dict[int, object]) -> bytes:
	"""Handle every complete frame in ``buffer``; return the incomplete tail."""
	while len(buffer) >= 4:
		length = int.from_bytes(buffer[:4], 'big')
		if len(buffer) < 4 + length:
			break
		frame, buffer = buffer[4 : 4 + length], buffer[4 + length :]
		try:
			msg = cloudpickle.loads(frame)
			action = msg.get('action')
			token = msg.get('token')
		except Exception:
			continue
		if action == 'add':
			cleanups[token] = msg.get('cleanup')
		elif action == 'remove':
			cleanups.pop(token, None)
		elif action == 'kill':
			cleanup = cleanups.pop(token, None)
			if cleanup is not None:
				_run_cleanup(cleanup)
			try:
				os.write(1, f'{token}\n'.encode())
			except OSError:
				pass
	return buffer


def main():
	# Dying on Ctrl+C, or on a SIGTERM aimed at the whole tree, is exactly the
	# case cleanups exist for. Own session (see the parent) keeps group-wide
	# signals away; this also covers the ones addressed to us directly. The
	# parent closing stdin is the only exit signal we honour.
	for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
		signal.signal(sig, signal.SIG_IGN)

	original_ppid = os.getppid()

	cleanups: dict[int, object] = {}
	buffer = b''
	stdin_fd = sys.stdin.fileno()

	while True:
		# A dying parent closes stdin, so EOF is the usual way out; the ppid
		# check only covers a parent that somehow leaked the write end.
		if os.getppid() != original_ppid:
			# Drain whatever it managed to send before dying.
			while True:
				data = _read(stdin_fd, 0)
				if not data:
					break
				buffer = _consume(buffer + data, cleanups)
			break

		data = _read(stdin_fd, 1.0)
		if data is None:
			break
		buffer = _consume(buffer + data, cleanups)

	# Tear down everything still registered (dangling cleanups).
	for cleanup in cleanups.values():
		_run_cleanup(cleanup)

	sys.exit(0)


if __name__ == '__main__':
	main()
