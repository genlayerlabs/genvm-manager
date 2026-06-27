#!/usr/bin/env python3
"""
Watchdog process for genvm-tool test.

Started by the test runner as a child process. It owns the cleanups (e.g.
``docker stop``) registered by the runner and is the single place that runs
them, so a container is always torn down through the watchdog:

- on a ``kill`` request: runs that one cleanup now and acknowledges it;
- on normal exit (stdin closed): runs every still-registered cleanup, killing
	whatever the runner left dangling;
- on unexpected death (detected via reparenting): kills the original process
	group, then runs every still-registered cleanup.

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


def main():
	original_ppid = os.getppid()
	original_pgrp = os.getpgrp()

	cleanups: dict[int, object] = {}
	buffer = b''
	stdin_fd = sys.stdin.fileno()
	reparented = False

	while True:
		if os.getppid() != original_ppid:
			reparented = True
			break

		try:
			ready, _, _ = select.select([stdin_fd], [], [], 1.0)
		except (ValueError, OSError):
			break

		if not ready:
			continue

		try:
			data = os.read(stdin_fd, 65536)
		except OSError:
			break
		if not data:
			# stdin closed - parent finished normally
			break

		buffer += data
		while len(buffer) >= 4:
			length = int.from_bytes(buffer[:4], 'big')
			if len(buffer) < 4 + length:
				break
			payload = buffer[4 + length :]
			frame, buffer = buffer[4 : 4 + length], payload
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

	if reparented:
		# Parent died. Move to our own group so we survive the group kill, then
		# take down the original group before running cleanups.
		try:
			os.setpgrp()
		except OSError:
			pass
		try:
			os.killpg(original_pgrp, signal.SIGKILL)
		except (ProcessLookupError, PermissionError, OSError):
			pass

	# Tear down everything still registered (dangling cleanups).
	for cleanup in cleanups.values():
		_run_cleanup(cleanup)

	sys.exit(0)


if __name__ == '__main__':
	main()
