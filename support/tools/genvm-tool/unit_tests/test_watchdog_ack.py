"""A watchdog that stops answering must not block the thread that asked it."""

import contextlib
import os
import time
import types

import pytest
from genvm_tool.tests.util import watchdog


@pytest.fixture
def pipe():
	read_fd, write_fd = os.pipe()
	reader = os.fdopen(read_fd, 'rb')
	try:
		yield reader, write_fd
	finally:
		reader.close()
		# a test may have closed the write end itself, to signal EOF
		with contextlib.suppress(OSError):
			os.close(write_fd)


def _dog(reader, *, timeout: float = 5.0) -> watchdog.Watchdog:
	dog = watchdog.Watchdog(types.SimpleNamespace(stdout=reader))
	dog.ACK_TIMEOUT = timeout
	return dog


def test_a_silent_watchdog_times_out(pipe):
	reader, _write_fd = pipe
	dog = _dog(reader, timeout=0.2)
	start = time.monotonic()
	# the write end stays open, so this can only end on the deadline
	assert dog._await_ack(1) is False
	assert time.monotonic() - start < 5


def test_the_matching_ack_is_accepted(pipe):
	reader, write_fd = pipe
	os.write(write_fd, b'7\n')
	assert _dog(reader)._await_ack(7) is True


def test_stale_acks_are_skipped(pipe):
	reader, write_fd = pipe
	os.write(write_fd, b'3\n7\n')
	assert _dog(reader)._await_ack(7) is True


def test_a_closed_pipe_is_not_mistaken_for_an_ack(pipe):
	reader, write_fd = pipe
	os.close(write_fd)
	assert _dog(reader)._await_ack(1) is False
