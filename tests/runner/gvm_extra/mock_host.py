import asyncio
import collections.abc
import contextlib
import pickle
import socket
from pathlib import Path

from origin.base_host import (
	Context,
	HostException,
	IHost,
	host_fns,
	host_loop_on,
	public_abi,
)
from origin.calldata import Address

type ResolveCallContractExecutorHook = collections.abc.Callable[
	[Address, public_abi.StorageView, int],
	bytes | None,
]


class MockStorage:
	_storages: dict[Address, dict[bytes, bytearray]]

	def __init__(self):
		self._storages = {}

	def read(self, account: Address, slot: bytes, index: int, le: int) -> bytes:
		res = self._storages.setdefault(account, {})
		res = res.setdefault(slot, bytearray())
		return res[index : index + le] + b'\x00' * (le - max(0, len(res) - index))

	def write(
		self,
		account: Address,
		slot: bytes,
		index: int,
		what: collections.abc.Buffer,
	) -> None:
		res = self._storages.setdefault(account, {})
		res = res.setdefault(slot, bytearray())
		what = memoryview(what)
		res.extend(b'\x00' * (index + len(what) - len(res)))
		memoryview(res)[index : index + len(what)] = what


_STOP_CONNECTIONS_TIMEOUT_S = 10.0
"""
Cap on how long ``MockHost.stop_connections`` waits for a cancelled task to
actually finish.

Cancellation only requests a ``CancelledError`` at the next await point; a
task stuck outside the event loop, or one that swallows it, would otherwise
hang the test teardown indefinitely.
"""


def _close_watched(sock: socket.socket) -> None:
	"""
	Closes a socket that an asyncio task may still be reading from.

	``Task.cancel`` only takes effect on the next loop iteration, so a task
	blocked in ``sock_accept``/``sock_recv`` deregisters its reader *after* a
	synchronous ``__exit__`` has closed the file descriptor -- by which point the
	number may already belong to someone else's socket, whose reader it then
	silently removes. Dropping the watch first keeps that from happening.
	"""
	with contextlib.suppress(Exception):
		asyncio.get_event_loop().remove_reader(sock.fileno())
	with contextlib.suppress(OSError):
		sock.close()


class MockHost(IHost):
	sock: socket.socket | None
	storage: MockStorage | None

	def __init__(
		self,
		*,
		path: str,
		storage_path_pre: Path,
		storage_path_post: Path,
		balances: dict[Address, int],
		running_address: Address,
		ctx: Context,
		expected_hello_data: bytes = b'',
		resolve_call_contract_executor_hook: ResolveCallContractExecutorHook | None = None,
	):
		self.running_address = running_address
		self.path = path
		self.storage_path_pre = storage_path_pre
		self.storage_path_post = storage_path_post
		self.storage = None
		self.sock = None
		self.sock_listener = None
		self.thread = None
		self.balances = balances
		self.nondet_disagreement_call_no = None
		self.expected_hello_data = expected_hello_data
		self.resolve_call_contract_executor_hook = resolve_call_contract_executor_hook
		self.ctx = ctx
		self._accept_task: asyncio.Task | None = None
		self._connection_tasks: list[asyncio.Task] = []
		self._accepted_sockets: list[socket.socket] = []

	def __enter__(self):
		self.created = False
		Path(self.path).unlink(missing_ok=True)
		self.thread_should_stop = False
		with open(self.storage_path_pre, 'rb') as f:
			self.storage = pickle.load(f)

		self.sock_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
		self.sock_listener.bind(self.path)
		self.sock_listener.setblocking(False)
		# A run that delegates across a major boundary spawns nested executors,
		# and each dials this same listener.
		self.sock_listener.listen(8)

		return self

	def __exit__(self, *_args):
		if self.storage is not None:
			with open(self.storage_path_post, 'wb') as f:
				pickle.dump(self.storage, f)
			self.storage = None
		if self._accept_task is not None:
			self._accept_task.cancel()
			self._accept_task = None
		for task in self._connection_tasks:
			task.cancel()
		self._connection_tasks.clear()
		for accepted in self._accepted_sockets:
			_close_watched(accepted)
		self._accepted_sockets.clear()
		self.sock = None
		if self.sock_listener is not None:
			_close_watched(self.sock_listener)
			self.sock_listener = None
		Path(self.path).unlink(missing_ok=True)

	async def notify_nondet_disagreement(self, call_no: int) -> None:
		self.nondet_disagreement_call_no = call_no
		pass

	async def loop_enter(self, cancellation: asyncio.Event):
		sock = await self._accept(cancellation)
		if sock is None:
			raise Exception('Program failed')
		self.sock = sock
		if self._accept_task is None:
			# Serve every later connection ourselves. Accepting stops when the
			# run ends, which is when `run_genvm` sets the cancellation event.
			self._accept_task = asyncio.create_task(self._accept_connections(cancellation))
		return sock

	async def _accept(self, cancellation: asyncio.Event) -> socket.socket | None:
		"""
		Accepts one connection, or returns ``None`` once the run is over.
		"""
		async_loop = asyncio.get_event_loop()
		assert self.sock_listener is not None

		interesting = asyncio.ensure_future(async_loop.sock_accept(self.sock_listener))
		canc = asyncio.ensure_future(cancellation.wait())

		try:
			done, _pending = await asyncio.wait(
				[canc, interesting], return_when=asyncio.FIRST_COMPLETED
			)
		finally:
			# Also runs when this task is cancelled mid-accept, which is the
			# normal way the background acceptor ends.
			for task in (interesting, canc):
				if not task.done():
					task.cancel()
		if canc in done:
			return None

		sock, _addr = interesting.result()
		sock.setblocking(False)
		self._accepted_sockets.append(sock)
		if self.expected_hello_data:
			actual = bytearray(len(self.expected_hello_data))
			view = memoryview(actual)
			pos = 0
			while pos < len(actual):
				read_len = await async_loop.sock_recv_into(sock, view[pos:])
				if read_len == 0:
					raise ConnectionResetError()
				pos += read_len
			if actual != self.expected_hello_data:
				raise AssertionError(
					f'host hello mismatch: expected {self.expected_hello_data!r}, got {bytes(actual)!r}'
				)
		return sock

	async def _accept_connections(self, cancellation: asyncio.Event) -> None:
		while True:
			sock = await self._accept(cancellation)
			if sock is None:
				return
			self._connection_tasks.append(
				asyncio.create_task(host_loop_on(self, sock, ctx=self.ctx))
			)

	async def stop_connections(self) -> None:
		"""
		Winds down the nested-connection loops, surfacing whatever they raised.

		Call it once the run is over and before leaving the ``with`` block, so a
		nested-side failure becomes a test failure instead of a cancelled task
		nobody awaited.

		Bounded by ``_STOP_CONNECTIONS_TIMEOUT_S`` via ``asyncio.wait`` --
		deliberately not ``wait_for``, which keeps re-awaiting a task that
		swallows ``CancelledError`` past its own timeout instead of returning.
		"""
		if self._accept_task is not None:
			task = self._accept_task
			task.cancel()
			_done, pending = await asyncio.wait([task], timeout=_STOP_CONNECTIONS_TIMEOUT_S)
			if pending:
				raise TimeoutError('accept task did not stop within stop_connections timeout')
			with contextlib.suppress(asyncio.CancelledError):
				task.result()
			self._accept_task = None
		for task in self._connection_tasks:
			if not task.done():
				task.cancel()
		pending_tasks = self._connection_tasks
		self._connection_tasks = []
		if pending_tasks:
			done, still_pending = await asyncio.wait(
				pending_tasks, timeout=_STOP_CONNECTIONS_TIMEOUT_S
			)
			if still_pending:
				raise TimeoutError(
					f'{len(still_pending)} nested host connection(s) did not stop '
					'within stop_connections timeout'
				)
			for task in done:
				with contextlib.suppress(asyncio.CancelledError, ConnectionResetError):
					task.result()

	async def storage_read(
		self,
		mode: public_abi.StorageView,
		address: bytes,
		slot: bytes,
		offset: int,
		le: int,
	) -> bytes:
		assert self.storage is not None
		return self.storage.read(Address(address), slot, offset, le)

	async def resolve_call_contract_executor(
		self,
		contract_address: Address,
		state_mode: public_abi.StorageView,
		advisory_major: int,
		/,
	) -> bytes | None:
		if self.resolve_call_contract_executor_hook is None:
			return None
		return self.resolve_call_contract_executor_hook(
			contract_address,
			state_mode,
			advisory_major,
		)

	async def get_remaining_time_fee_gen_wei(self) -> int:
		return 2**32

	async def external_call(self, address: bytes, calldata: bytes, /) -> bytes:
		raise HostException(host_fns.Errors.EVM_REVERTED)

	async def consume_time_fee_gen_wei(self, time_fee_gen_wei: int):
		pass

	async def get_balance_gen_wei(self, address: bytes) -> int:
		return self.balances.get(Address(address), 0)
