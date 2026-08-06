"""Cross-major CallContract system test.

The case owns its manager because it exercises manager cancellation and
deadlines while executor processes are nested beneath it.
"""

import asyncio
import contextlib
import json
import os
import pickle
import socket
import textwrap
import typing
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import genvm_tool.tests
import genvm_tool.tests.stage.collection
import origin.base_host as base_host
import origin.calldata as gvm_calldata
from gvm_extra.mock_host import MockHost, MockStorage
from origin import public_abi
from origin.calldata import Address

PY_GENLAYER_V03 = '9b8kjyda2ycxyq4ea6g4yfpnydxhd52gqba5rb8dw7krkh5mn9p0'
PY_GENLAYER_V02 = '1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6'

ADDR_CALLER_V03 = Address('0x' + '31' * 20)
ADDR_CALLER_V02 = Address('0x' + '21' * 20)
ADDR_CALLEE_V03 = Address('0x' + '32' * 20)
ADDR_CALLEE_V02 = Address('0x' + '22' * 20)
ADDR_BUSY_V02 = Address('0x' + '23' * 20)
ADDR_LOOP_V03 = Address('0x' + '33' * 20)
ADDR_LOOP_V02 = Address('0x' + '24' * 20)
ADDR_SIGNER_V03 = Address('0x' + '34' * 20)
ADDR_SIGNER_CALLER_V02 = Address('0x' + '25' * 20)
ADDR_STRUCTURED_V03 = Address('0x' + '35' * 20)
ADDR_BLOB_V03 = Address('0x' + '36' * 20)
ADDR_BLOB_CALLER_V02 = Address('0x' + '26' * 20)
ADDR_UERR_V03 = Address('0x' + '37' * 20)
ADDR_UERR_V02 = Address('0x' + '27' * 20)
ADDR_STATE_CALLER_V03 = Address('0x' + '38' * 20)
ADDR_CHAIN_V03 = Address('0x' + '39' * 20)
ADDR_CHAIN_V02 = Address('0x' + '28' * 20)

SENDER = Address('0x' + '01' * 20)
# Larger than the manager cap the test configures below, so the nested envelope
# cannot fit through the socketpair.
"""Client frame cap for `/ws`; kept low so a nested envelope can exceed it."""
MANAGER_MAX_MESSAGE_BYTES = 1 * 1024 * 1024
BLOB_BYTES = 2 * 1024 * 1024
TIMESTAMP = '2024-11-26T06:42:42.424242Z'


def _contract_source(line: int, body: str) -> bytes:
	runner = PY_GENLAYER_V03 if line == 3 else PY_GENLAYER_V02
	return (
		f'# {{ "Depends": "py-genlayer:{runner}" }}\n' + textwrap.dedent(body).lstrip()
	).encode()


CALLER_V03 = _contract_source(
	3,
	"""
	import genlayer as gl
	from genlayer.types import Address


	class Contract(gl.contract.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def call(self, target: Address) -> int:
			return gl.contract.get_at(target).view().answer()
	""",
)

CALLER_V02 = _contract_source(
	2,
	"""
	from genlayer import *


	class Contract(gl.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def call(self, target: Address) -> int:
			return gl.get_contract_at(target).view().answer()
	""",
)

STATE_CALLER_V03 = _contract_source(
	3,
	"""
	import genlayer as gl
	from genlayer.types import Address


	class Contract(gl.contract.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def call(self, target: Address, final: bool) -> int:
			mode = (
				gl.vm.public_abi.StorageType.LATEST_FINAL
				if final
				else gl.vm.public_abi.StorageType.LATEST_NON_FINAL
			)
			return gl.contract.get_at(target).view(state=mode).answer()
	""",
)

CALLEE_V03 = _contract_source(
	3,
	"""
	import genlayer as gl


	class Contract(gl.contract.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def answer(self) -> int:
			return 303
	""",
)

CALLEE_V02 = _contract_source(
	2,
	"""
	from genlayer import *


	class Contract(gl.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def answer(self) -> int:
			return 202
	""",
)

SIGNER_V03 = _contract_source(
	3,
	"""
	import genlayer as gl


	class Contract(gl.contract.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def signer(self) -> str:
			return gl.message.signer_address.as_hex
	""",
)

SIGNER_CALLER_V02 = _contract_source(
	2,
	"""
	from genlayer import *


	class Contract(gl.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def signer(self, target: Address) -> str:
			return gl.get_contract_at(target).view().signer()
	""",
)

STRUCTURED_ERROR_V03 = _contract_source(
	3,
	"""
	import genlayer as gl


	class Contract(gl.contract.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def answer(self) -> int:
			gl.vm.UserError.immediate({'code': 7})
	""",
)

BLOB_V03 = _contract_source(
	3,
	"""
	import genlayer as gl


	class Contract(gl.contract.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def sink(self, blob: str) -> int:
			return len(blob)
	""",
)

BLOB_CALLER_V02 = _contract_source(
	2,
	"""
	from genlayer import *


	class Contract(gl.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def send(self, target: Address, size: int) -> int:
			return gl.get_contract_at(target).view().sink('x' * size)
	""",
)

USER_ERROR_V03 = _contract_source(
	3,
	"""
	import genlayer as gl


	class Contract(gl.contract.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def answer(self) -> int:
			gl.vm.UserError.immediate('boom')
	""",
)

USER_ERROR_V02 = _contract_source(
	2,
	"""
	from genlayer import *


	class Contract(gl.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def answer(self) -> int:
			raise gl.vm.UserError('boom')
	""",
)

CHAIN_V03 = _contract_source(
	3,
	"""
	import genlayer as gl
	from genlayer.types import Address


	class Contract(gl.contract.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def hop(self, depth: int, other: Address) -> int:
			if depth <= 0:
				return 3
			me = gl.message.contract_address
			return 3 + gl.contract.get_at(other).view().hop(depth - 1, me)
	""",
)

CHAIN_V02 = _contract_source(
	2,
	"""
	from genlayer import *


	class Contract(gl.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def hop(self, depth: int, other: Address) -> int:
			if depth <= 0:
				return 2
			me = gl.message.contract_address
			return 2 + gl.get_contract_at(other).view().hop(depth - 1, me)
	""",
)

BUSY_V02 = _contract_source(
	2,
	"""
	from genlayer import *


	class Contract(gl.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def answer(self) -> int:
			while True:
				pass
	""",
)

LOOP_V03 = _contract_source(
	3,
	"""
	import genlayer as gl
	from genlayer.types import Address


	class Contract(gl.contract.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def loop(self, remaining: int, first: Address, second: Address) -> int:
			if remaining > 0:
				return gl.contract.get_at(first).view().loop(
					remaining - 1, first, second
				)
			return gl.contract.get_at(second).view().loop(first, second)
	""",
)

LOOP_V02 = _contract_source(
	2,
	"""
	from genlayer import *


	class Contract(gl.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def loop(self, first: Address, second: Address) -> int:
			return gl.get_contract_at(first).view().loop(0, first, second)
	""",
)


def _wat_data(data: bytes) -> str:
	return ''.join(f'\\{byte:02x}' for byte in data)


def _loop_wat(target: Address) -> str:
	call = gvm_calldata.encode(
		{
			'CallContract': {
				'address': target,
				'calldata': {},
				'state': public_abi.StorageType.LATEST_NON_FINAL,
			}
		}
	)
	depth = gvm_calldata.encode({'Return': 'depth'})
	failure = gvm_calldata.encode({'UserError': 'unexpected nested result'})
	depth_offset = len(call)
	failure_offset = depth_offset + len(depth)
	return f"""
	(module
		(func $gl_call (import "genlayer_sdk" "gl_call")
			(param i32 i32 i32) (result i32))
		(func $fd_read (import "wasi_snapshot_preview1" "fd_read")
			(param i32 i32 i32 i32) (result i32))
		(memory (export "memory") 1)
		(data (i32.const 0) "{_wat_data(call)}")
		(data (i32.const {depth_offset}) "{_wat_data(depth)}")
		(data (i32.const {failure_offset}) "{_wat_data(failure)}")

		(func (export "_start")
			;; The iovec at 4096 reads the child result into 8192.
			(i32.store (i32.const 4096) (i32.const 8192))
			(i32.store (i32.const 4100) (i32.const 1024))
			(if
				(call $gl_call
					(i32.const 0) (i32.const {len(call)}) (i32.const 4108))
				(then unreachable))
			(if
				(call $fd_read
					(i32.load (i32.const 4108))
					(i32.const 4096) (i32.const 1) (i32.const 4112))
				(then unreachable))
			(if
				(i32.or
					(i32.eq (i32.load8_u (i32.const 8192)) (i32.const 0))
					(i32.eq (i32.load8_u (i32.const 8192)) (i32.const 2)))
				(then
					(drop
						(call $gl_call
							(i32.const {depth_offset})
							(i32.const {len(depth)})
							(i32.const 4108)))
					unreachable)
				(else
					(drop
						(call $gl_call
							(i32.const {failure_offset})
							(i32.const {len(failure)})
							(i32.const 4108)))
					unreachable)
			)
		)
	)
	"""


class _TestContext(base_host.Context):
	def __init__(self, logger: base_host.Logger):
		self.logger = logger
		self.stats: dict[str, typing.Any] = {}

	def on_genvm_success(self): ...
	def on_genvm_failure(self): ...

	def add_stat(self, key: str, value: typing.Any, /):
		self.stats[key] = value


class ManagerProc:
	def __init__(self, *, build_dir: Path, work_dir: Path):
		self.build_dir = build_dir
		self.work_dir = work_dir
		self.process: asyncio.subprocess.Process | None = None
		self.log_file = None
		self.port = self._unused_port()

	@staticmethod
	def _unused_port() -> int:
		with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
			sock.bind(('127.0.0.1', 0))
			return sock.getsockname()[1]

	@property
	def uri(self) -> str:
		return f'http://127.0.0.1:{self.port}'

	async def __aenter__(self):
		self.work_dir.mkdir(parents=True, exist_ok=True)
		config_path = self.work_dir / 'genvm-manager.yaml'
		config_path.write_text(
			'\n'.join(
				[
					'threads: 4',
					'blocking_threads: 48',
					'log_disable: tracing*,polling*,tungstenite*,tokio_tungstenite*',
					'log_level: info',
					f'manifest_path: {self.build_dir / "out" / "data" / "manifest.yaml"}',
					'permits: 2',
					'execution_retention: 30s',
					f'max_message_bytes: {MANAGER_MAX_MESSAGE_BYTES}',
					'',
				]
			)
		)
		self.log_file = open(self.work_dir / 'manager.log', 'wb')
		self.process = await asyncio.subprocess.create_subprocess_exec(
			str(self.build_dir / 'out' / 'bin' / 'genvm-modules'),
			'manager',
			'--port',
			str(self.port),
			'--die-with-parent',
			'--config',
			str(config_path),
			stdin=asyncio.subprocess.DEVNULL,
			stdout=self.log_file,
			stderr=self.log_file,
			start_new_session=True,
		)
		await self._wait_healthy()
		return self

	async def __aexit__(self, *_args):
		if self.process is not None:
			try:
				self.process.terminate()
				await asyncio.wait_for(self.process.wait(), timeout=5)
			except asyncio.TimeoutError:
				self.process.kill()
				await self.process.wait()
		if self.log_file is not None:
			self.log_file.close()

	async def _wait_healthy(self):
		last_error: BaseException | None = None
		for _ in range(80):
			if self.process is not None and self.process.returncode is not None:
				raise RuntimeError(f'manager exited early with {self.process.returncode}')
			try:
				async with aiohttp.request(
					'GET',
					f'{self.uri}/status',
					timeout=aiohttp.ClientTimeout(total=1),
				) as response:
					if response.status == 200:
						return
			except BaseException as exc:
				last_error = exc
			await asyncio.sleep(0.1)
		raise TimeoutError(f'manager did not become healthy: {last_error}')


def _message(address: Address, *, is_init: bool) -> base_host.Message:
	return {
		'contract_address': address,
		'sender_address': SENDER,
		'origin_address': SENDER,
		'signer_address': SENDER,
		'chain_id': 61999,
		'value': 0,
		'is_init': is_init,
		'datetime': TIMESTAMP,
	}


def _calldata(method: str, *args: typing.Any) -> bytes:
	return gvm_calldata.encode({'': method, 'args': list(args)})


def _apply_storage_changes(
	storage: MockStorage,
	address: Address,
	changes: list[tuple[bytes, bytes]],
) -> None:
	for key, value in changes:
		assert len(key) == 36
		storage.write(
			address,
			key[:32],
			int.from_bytes(key[32:], byteorder='big') * 32,
			value,
		)


def _descendants(pid: int) -> set[int]:
	children: dict[int, set[int]] = {}
	for stat_path in Path('/proc').glob('[0-9]*/stat'):
		try:
			raw = stat_path.read_text()
			parent = int(raw[raw.rfind(')') + 2 :].split()[1])
			child = int(stat_path.parent.name)
		except (FileNotFoundError, ProcessLookupError, ValueError):
			continue
		children.setdefault(parent, set()).add(child)
	result: set[int] = set()
	pending = [pid]
	while pending:
		parent = pending.pop()
		for child in children.get(parent, ()):
			if child not in result:
				result.add(child)
				pending.append(child)
	return result


async def _wait_for_descendants(
	pid: int, expected: int, timeout: float = 10
) -> set[int]:
	async with asyncio.timeout(timeout):
		while True:
			current = _descendants(pid)
			if len(current) >= expected:
				return current
			await asyncio.sleep(0.05)


async def _wait_for_no_descendants(pid: int, timeout: float = 10) -> None:
	async with asyncio.timeout(timeout):
		while _descendants(pid):
			await asyncio.sleep(0.05)


@dataclass
class CrossMajorCase(genvm_tool.tests.test.Case):
	description: genvm_tool.tests.test.Description
	shared: genvm_tool.tests.SharedContext

	async def into_steps(self) -> list[genvm_tool.tests.exec.step.Step]:
		return [CrossMajorStep(self)]


class CrossMajorStep(genvm_tool.tests.exec.step.Python):
	def __init__(self, case: CrossMajorCase):
		self.case = case
		self.phase = 'start'
		self.notes: list[typing.Any] = []

	def to_str(self) -> str:
		return '<cross-major CallContract system test>'

	async def run(
		self, previous_results: list[typing.Any]
	) -> genvm_tool.tests.test.Result:
		try:
			await self._run_all()
			return genvm_tool.tests.test.Result(
				passed=True, context={'notes': self.notes}, elapsed_seconds=0
			)
		except BaseException as exc:
			return genvm_tool.tests.test.Result(
				passed=False,
				context={'phase': self.phase, 'error': repr(exc)},
				elapsed_seconds=0,
			)

	async def _run_all(self):
		root = self.case.shared.root_dir
		build_info = json.loads((root / 'build' / 'info.json').read_text())
		self.build_dir = Path(build_info['build_dir'])
		self.versions = {
			2: build_info['executor_versions']['v0.2'],
			3: build_info['executor_versions']['v0.3'],
		}
		work_dir = self.case.shared.artifacts_dir / 'cross-major'
		self.storage_path = work_dir / 'storage.pickle'
		work_dir.mkdir(parents=True, exist_ok=True)
		with open(self.storage_path, 'wb') as storage_file:
			pickle.dump(MockStorage(), storage_file)

		async with ManagerProc(
			build_dir=self.build_dir,
			work_dir=work_dir / 'manager',
		) as manager:
			self.manager = manager

			self.phase = 'deploy fixtures'
			await self._deploy(3, ADDR_CALLER_V03, CALLER_V03)
			await self._deploy(2, ADDR_CALLER_V02, CALLER_V02)
			await self._deploy(3, ADDR_CALLEE_V03, CALLEE_V03)
			await self._deploy(2, ADDR_CALLEE_V02, CALLEE_V02)
			await self._deploy(2, ADDR_BUSY_V02, BUSY_V02)
			await self._deploy(3, ADDR_LOOP_V03, LOOP_V03)
			await self._deploy(2, ADDR_LOOP_V02, LOOP_V02)
			await self._deploy(3, ADDR_SIGNER_V03, SIGNER_V03)
			await self._deploy(2, ADDR_SIGNER_CALLER_V02, SIGNER_CALLER_V02)
			await self._deploy(3, ADDR_STRUCTURED_V03, STRUCTURED_ERROR_V03)
			await self._deploy(3, ADDR_BLOB_V03, BLOB_V03)
			await self._deploy(2, ADDR_BLOB_CALLER_V02, BLOB_CALLER_V02)
			await self._deploy(3, ADDR_UERR_V03, USER_ERROR_V03)
			await self._deploy(2, ADDR_UERR_V02, USER_ERROR_V02)
			await self._deploy(3, ADDR_STATE_CALLER_V03, STATE_CALLER_V03)
			await self._deploy(3, ADDR_CHAIN_V03, CHAIN_V03)
			await self._deploy(2, ADDR_CHAIN_V02, CHAIN_V02)
			loop_v03 = await self._compile_wat(
				_loop_wat(ADDR_LOOP_V02), work_dir / 'loop-v03'
			)
			loop_v02 = await self._compile_wat(
				_loop_wat(ADDR_LOOP_V03), work_dir / 'loop-v02'
			)
			self._replace_code(ADDR_LOOP_V03, LOOP_V03, loop_v03)
			self._replace_code(ADDR_LOOP_V02, LOOP_V02, loop_v02)

			self.phase = 'v0.3 to v0.2'
			forward = await self._call(
				3,
				ADDR_CALLER_V03,
				ADDR_CALLEE_V02,
				lambda address, _state, _major: (
					self._route(2) if address == ADDR_CALLEE_V02 else None
				),
			)
			assert forward.result_kind == public_abi.ResultCode.RETURN, forward
			assert forward.result_data == 202, forward
			assert len(forward.execution_hash) == 32, forward.execution_hash
			assert forward.execution_hash.hex() == (
				'c0e5932da9b2e9bf79579079f1fec46f74b83f02114bcd2f711a6798ee20bdd6'
			), forward.execution_hash.hex()

			self.phase = 'v0.2 to v0.3'
			backward = await self._call(
				2,
				ADDR_CALLER_V02,
				ADDR_CALLEE_V03,
				lambda address, _state, _major: (
					self._route(3) if address == ADDR_CALLEE_V03 else None
				),
			)
			assert backward.result_kind == public_abi.ResultCode.RETURN, backward
			assert backward.result_data == 303, backward
			assert len(backward.execution_hash) == 32, backward.execution_hash
			assert backward.execution_hash.hex() == (
				'6aad898ed66c94e7bc9fc1f6bb7d615b9b4cf01d1e1205ca0c640712a8c6792d'
			), backward.execution_hash.hex()

			self.phase = 'v0.2 forwards the signer it never exposes'
			await self._assert_signer_preserved()

			self.phase = 'a nested envelope is not bounded by the websocket cap'
			await self._assert_envelope_ignores_websocket_cap()

			self.phase = 'a structured user error survives the boundary'
			await self._assert_structured_user_error()

			self.phase = 'null preserves same-major behavior'
			resolved: list[tuple[Address, int]] = []

			def resolve_null(address, _state, advisory_major):
				resolved.append((address, advisory_major))
				return None

			same_major = await self._call(
				3,
				ADDR_CALLER_V03,
				ADDR_CALLEE_V03,
				resolve_null,
			)
			assert same_major.result_kind == public_abi.ResultCode.RETURN, same_major
			assert same_major.result_data == 303, same_major
			assert resolved == [(ADDR_CALLEE_V03, 0)], resolved

			self.phase = 'null preserves mismatch behavior'
			mismatch = await self._call(
				3,
				ADDR_CALLER_V03,
				ADDR_CALLEE_V02,
				lambda _address, _state, _major: None,
			)
			assert mismatch.result_kind == public_abi.ResultCode.VM_ERROR, mismatch
			assert str(mismatch.result_data).startswith(
				str(public_abi.VmError.invalid_contract().absent_runner_comment())
			), mismatch.result_data

			self.phase = 'a host that does not hook is never asked'
			await self._assert_unhooked_stays_in_process()

			self.phase = 'leader, validator and sync agree on the nested hash'
			await self._assert_lvs_agrees()

			self.phase = 'the route a host picks does not change the hash'
			await self._assert_route_is_hash_invariant()

			self.phase = 'a plain user error survives the boundary'
			await self._assert_plain_user_error()

			self.phase = 'the state mode a caller picks crosses the boundary'
			await self._assert_state_mode_crosses()

			self.phase = 'a deep alternating chain stays deterministic'
			await self._assert_deep_chain()

			self.phase = 'a starved fuel budget never becomes an internal error'
			await self._assert_starved_fuel()

			self.phase = 'a contract cannot manufacture an internal error'
			await self._assert_child_failures_are_canonical()

			self.phase = 'cancel kills process chain'
			await self._assert_chain_stopped('cancel')

			self.phase = 'deadline kills process chain'
			await self._assert_chain_stopped('deadline')

			self.phase = 'a nested chain that runs out of time reports timeout'
			await self._assert_nested_deadline_is_timeout()

			self.phase = 'resolve loop bottoms out on the recursion budget'
			await self._assert_resolve_loop_recursion()

	async def _new_host(
		self,
		name: str,
		running_address: Address,
		resolve_hook=None,
		read_log: list[tuple[Address, public_abi.StorageType]] | None = None,
		host_fuel: int | None = None,
	) -> MockHost:
		ctx = _TestContext(self.case.shared.logger)
		host_path = (
			Path('/tmp')
			/ f'gvm-cross-major-{os.getpid()}'
			/ f'{name[:20]}-{os.urandom(4).hex()}.sock'
		)
		host_path.parent.mkdir(parents=True, exist_ok=True)
		host = MockHost(
			ctx=ctx,
			path=str(host_path),
			storage_path_pre=self.storage_path,
			storage_path_post=self.storage_path,
			balances={},
			running_address=running_address,
			resolve_callcontract_executor_hook=resolve_hook,
		)
		if read_log is not None:
			original = host.storage_read

			async def logged(mode, account, slot, index, le, /):
				read_log.append((Address(account), mode))
				return await original(mode, account, slot, index, le)

			host.storage_read = logged  # type: ignore[method-assign]
		if host_fuel is not None:

			async def remaining_fuel_as_gen() -> int:
				return host_fuel

			host.remaining_fuel_as_gen = remaining_fuel_as_gen  # type: ignore[method-assign]
		return host

	async def _execute(
		self,
		*,
		name: str,
		line: int,
		address: Address,
		calldata: bytes,
		code: bytes | None,
		is_init: bool,
		resolve_hook=None,
		timeout: float = 30,
		debug_initial_recursion: int | None = None,
		is_sync: bool = True,
		leader_nondet_results: list[bytes] | None = None,
		apply_changes: bool = True,
		read_log: list[tuple[Address, public_abi.StorageType]] | None = None,
		host_fuel: int | None = None,
		hook_cross_contract_calls: bool = True,
	):
		host = await self._new_host(
			name, address, resolve_hook, read_log=read_log, host_fuel=host_fuel
		)
		ctx = host.ctx
		with host as mock_host:
			try:
				async with base_host.ManagerClient(self.manager.uri) as manager_client:
					result = await base_host.run_genvm(
						mock_host,
						manager_uri=self.manager.uri,
						manager_client=manager_client,
						ctx=ctx,
						is_sync=is_sync,
						leader_nondet_results=leader_nondet_results,
						message=_message(address, is_init=is_init),
						host_data='{"node_address":"test","tx_id":"cross-major"}',
						host='unix://' + mock_host.path,
						code=code,
						calldata=calldata,
						timeout=timeout,
						debug_mode='unsafe',
						unsafe_overrides=base_host.UnsafeOverrides(
							reroute_to=self.versions[line],
							initial_recursion=debug_initial_recursion,
						),
						request_extra={
							'no_modules': True,
							'hook_cross_contract_calls': hook_cross_contract_calls,
						},
						bucket_totals=[2**200] * 20,
					)
				if apply_changes and result.result_kind == public_abi.ResultCode.RETURN:
					assert mock_host.storage is not None
					_apply_storage_changes(
						mock_host.storage,
						address,
						result.result_storage_changes,
					)
				return result
			finally:
				await host.stop_connections()

	async def _deploy(self, line: int, address: Address, code: bytes):
		result = await self._execute(
			name=f'deploy-{line}-{address.as_bytes.hex()}',
			line=line,
			address=address,
			calldata=gvm_calldata.encode({}),
			code=code,
			is_init=True,
		)
		assert result.result_kind == public_abi.ResultCode.RETURN, result
		assert len(result.execution_hash) == 32, result.execution_hash

	async def _compile_wat(self, wat: str, path: Path) -> bytes:
		path.mkdir(parents=True, exist_ok=True)
		wat_path = path / 'contract.wat'
		wasm_path = path / 'contract.wasm'
		wat_path.write_text(wat)
		process = await asyncio.create_subprocess_exec(
			'wat2wasm',
			'--enable-annotations',
			'-o',
			str(wasm_path),
			str(wat_path),
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.PIPE,
		)
		stdout, stderr = await process.communicate()
		assert process.returncode == 0, (stdout, stderr)
		return wasm_path.read_bytes()

	def _replace_code(self, address: Address, old_code: bytes, new_code: bytes) -> None:
		with open(self.storage_path, 'rb') as storage_file:
			storage: MockStorage = pickle.load(storage_file)
		slots = storage._storages[address]
		code_slots = [
			slot
			for slot, data in slots.items()
			if int.from_bytes(data[:4], byteorder='little') == len(old_code)
			and bytes(data[4 : 4 + len(old_code)]) == old_code
		]
		assert len(code_slots) == 1, code_slots
		storage.write(
			address,
			code_slots[0],
			0,
			len(new_code).to_bytes(4, byteorder='little') + new_code,
		)
		with open(self.storage_path, 'wb') as storage_file:
			pickle.dump(storage, storage_file)

	def _route(self, line: int) -> bytes:
		"""Routing payload placing a callee on `line`'s executor.

		It names the directory rather than the major, because a major cannot
		pick between the live lines: every manifest key is semver major 0, so
		`{'kind': 'major'}` resolves to the newest line whatever it asks for.
		"""
		return gvm_calldata.encode({'kind': 'version', 'version': self.versions[line]})

	async def _call(self, line: int, caller: Address, target: Address, resolve_hook):
		return await self._execute(
			name=f'call-{line}-{caller.as_bytes.hex()}-{target.as_bytes.hex()}',
			line=line,
			address=caller,
			calldata=_calldata('call', target),
			code=None,
			is_init=False,
			resolve_hook=resolve_hook,
		)

	def _raw_request(
		self,
		*,
		line: int,
		address: Address,
		host_path: str,
		deadline: str,
	) -> dict[str, typing.Any]:
		return {
			'selector': {'kind': 'major', 'major': base_host.UNDEPLOYED_MAJOR},
			'message': _message(address, is_init=False),
			'is_sync': True,
			'debug_mode': 'unsafe',
			'host_data': '{"node_address":"test","tx_id":"cross-major-lifecycle"}',
			'max_execution_minutes': 20,
			'timestamp': TIMESTAMP,
			'host': 'unix://' + host_path,
			'extra_args': [],
			'code': None,
			'calldata': _calldata('call', ADDR_BUSY_V02),
			'leader_nondet_results': None,
			'bucket_totals': [2**200] * 20,
			'gas_data': base_host.DEFAULT_GAS_DATA,
			'message_fee_allocation': [],
			'initial_time_units_allocation': 60,
			'no_modules': True,
			'hook_cross_contract_calls': True,
			'unsafe_overrides': base_host.UnsafeOverrides(
				reroute_to=self.versions[line]
			).as_request_field(),
			'deadline': deadline,
			'host_hello_data': [],
		}

	async def _assert_chain_stopped(self, mode: typing.Literal['cancel', 'deadline']):
		nested_resolved = asyncio.Event()

		def resolve(address, _state, _major):
			if address == ADDR_BUSY_V02:
				nested_resolved.set()
				return self._route(2)
			return None

		host = await self._new_host(f'lifecycle-{mode}', ADDR_CALLER_V03, resolve)
		cancellation = asyncio.Event()
		assert self.manager.process is not None
		manager_pid = self.manager.process.pid
		with host as mock_host:
			host_task = asyncio.create_task(
				base_host.host_loop(mock_host, cancellation, ctx=host.ctx)
			)
			try:
				async with base_host.ManagerClient(self.manager.uri) as client:
					state = await client.run(
						self._raw_request(
							line=3,
							address=ADDR_CALLER_V03,
							host_path=mock_host.path,
							deadline='30s' if mode == 'cancel' else '3s',
						)
					)
					await asyncio.wait_for(nested_resolved.wait(), timeout=10)
					await _wait_for_descendants(manager_pid, 2)
					if mode == 'cancel':
						await client.cancel(state.boot_id, state.genvm_id)
					terminal = await asyncio.wait_for(client.wait_terminal(state), timeout=10)
					assert terminal['finished']['cause'] == (
						'cancelled' if mode == 'cancel' else 'deadline'
					), terminal
					await _wait_for_no_descendants(manager_pid)
					await client.ack(state.boot_id, state.genvm_id)
			finally:
				cancellation.set()
				with contextlib.suppress(BaseException):
					await host_task
				await host.stop_connections()

	async def _assert_signer_preserved(self):
		"""This line's contract-facing message has no signer, but the executor
		still receives one and must hand it to a callee that does expose it."""
		result = await self._execute(
			name='signer-v02-to-v03',
			line=2,
			address=ADDR_SIGNER_CALLER_V02,
			calldata=_calldata('signer', ADDR_SIGNER_V03),
			code=None,
			is_init=False,
			resolve_hook=lambda address, _state, _major: (
				self._route(3) if address == ADDR_SIGNER_V03 else None
			),
		)
		assert result.result_kind == public_abi.ResultCode.RETURN, result
		assert result.result_data == SENDER.as_hex, (result.result_data, SENDER.as_hex)

	async def _assert_structured_user_error(self):
		"""A callee's user error is contract data, so it must stay contract-visible
		on the caller. v0.2 user errors are strings, so a structured payload cannot
		be carried across as one and degrades to the vm error naming the boundary
		that could not represent it — never an internal error."""
		result = await self._execute(
			name='structured-error-v02-to-v03',
			line=2,
			address=ADDR_CALLER_V02,
			calldata=_calldata('call', ADDR_STRUCTURED_V03),
			code=None,
			is_init=False,
			resolve_hook=lambda address, _state, _major: (
				self._route(3) if address == ADDR_STRUCTURED_V03 else None
			),
		)
		assert result.result_kind == public_abi.ResultCode.VM_ERROR, result
		expected = str(public_abi.VmError.invalid_contract().major_mismatch())
		assert result.result_data == expected, (result.result_data, expected)

	async def _assert_envelope_ignores_websocket_cap(self):
		"""A contract picks the calldata it forwards, so nothing an operator
		configures may decide whether the call goes through. This manager runs
		with `max_message_bytes` deliberately below the envelope this builds: the
		cap bounds client frames on `/ws`, never the executor socketpair."""
		assert BLOB_BYTES > MANAGER_MAX_MESSAGE_BYTES
		result = await self._execute(
			name='large-envelope-v02-to-v03',
			line=2,
			address=ADDR_BLOB_CALLER_V02,
			calldata=_calldata('send', ADDR_BLOB_V03, BLOB_BYTES),
			code=None,
			is_init=False,
			resolve_hook=lambda address, _state, _major: (
				self._route(3) if address == ADDR_BLOB_V03 else None
			),
			timeout=60,
		)
		assert result.result_kind == public_abi.ResultCode.RETURN, result
		assert result.result_data == BLOB_BYTES, result

	async def _assert_nested_deadline_is_timeout(self):
		"""The chain dies on a signal, so no executor reports anything. What the
		caller must still observe is a timeout, not the internal error the dead
		boundary would otherwise produce."""
		result = await self._execute(
			name='deadline-v03-to-v02',
			line=3,
			address=ADDR_CALLER_V03,
			calldata=_calldata('call', ADDR_BUSY_V02),
			code=None,
			is_init=False,
			resolve_hook=lambda address, _state, _major: (
				self._route(2) if address == ADDR_BUSY_V02 else None
			),
			timeout=10,
		)
		assert result.result_kind == public_abi.ResultCode.VM_ERROR, result
		assert result.result_data == str(public_abi.VmError.timeout()), result

	async def _lvs(
		self, *, name: str, line: int, address: Address, calldata: bytes, resolve_hook
	):
		"""Run the same step as leader, validator and sync run.

		The three must agree on the execution hash; anything else is a
		determinism violation an honest validator would see as a disagreement.
		"""

		async def one(suffix: str, **kwargs):
			return await self._execute(
				name=f'{name}-{suffix}',
				line=line,
				address=address,
				calldata=calldata,
				code=None,
				is_init=False,
				resolve_hook=resolve_hook,
				apply_changes=False,
				**kwargs,
			)

		leader = await one('leader', is_sync=False)
		validator = await one(
			'validator', is_sync=False, leader_nondet_results=leader.result_nondet_results
		)
		sync = await one('sync', is_sync=True)
		return leader, validator, sync

	async def _assert_lvs_agrees(self):
		# A leader/validator run that silently degraded to a sync one would make
		# the agreement below vacuous, so first pin that the validator mode is
		# real: handing it a leader result nothing consumes must be rejected.
		bogus = await self._execute(
			name='lvs-mode-is-real',
			line=3,
			address=ADDR_CALLER_V03,
			calldata=_calldata('call', ADDR_CALLEE_V02),
			code=None,
			is_init=False,
			resolve_hook=lambda address, _state, _major: (
				self._route(2) if address == ADDR_CALLEE_V02 else None
			),
			apply_changes=False,
			is_sync=False,
			leader_nondet_results=[gvm_calldata.encode({})],
		)
		assert bogus.result_kind == public_abi.ResultCode.VM_ERROR, bogus

		for suffix, line, caller, callee, major in (
			('fwd', 3, ADDR_CALLER_V03, ADDR_CALLEE_V02, 2),
			('bwd', 2, ADDR_CALLER_V02, ADDR_CALLEE_V03, 3),
		):
			target = callee
			leader, validator, sync = await self._lvs(
				name=f'lvs-{suffix}',
				line=line,
				address=caller,
				calldata=_calldata('call', callee),
				resolve_hook=lambda address, _state, _major, target=target, major=major: (
					self._route(major) if address == target else None
				),
			)
			for label, run in (('leader', leader), ('validator', validator), ('sync', sync)):
				assert run.result_kind == public_abi.ResultCode.RETURN, (suffix, label, run)
				assert len(run.execution_hash) == 32, (suffix, label, run.execution_hash)
			assert leader.execution_hash == validator.execution_hash, (
				suffix,
				leader.execution_hash.hex(),
				validator.execution_hash.hex(),
			)
			assert leader.execution_hash == sync.execution_hash, (
				suffix,
				leader.execution_hash.hex(),
				sync.execution_hash.hex(),
			)

	async def _assert_unhooked_stays_in_process(self):
		"""Without `hook_cross_contract_calls` the manager answers the resolve
		method itself, so a host is never consulted even when the callee really
		does live on another major, and the call stays in-process."""
		asked: list[Address] = []

		def resolve(address, _state, _major):
			asked.append(address)
			return self._route(2)

		result = await self._execute(
			name='unhooked-cross-major',
			line=3,
			address=ADDR_CALLER_V03,
			calldata=_calldata('call', ADDR_CALLEE_V02),
			code=None,
			is_init=False,
			resolve_hook=resolve,
			apply_changes=False,
			hook_cross_contract_calls=False,
		)
		assert asked == [], asked
		# The in-process outcome for a v0.2 callee, identical to answering null.
		assert result.result_kind == public_abi.ResultCode.VM_ERROR, result
		assert str(result.result_data).startswith(
			str(public_abi.VmError.invalid_contract().absent_runner_comment())
		), result.result_data

	async def _assert_route_is_hash_invariant(self):
		"""A host picks the route, a contract does not. A same-major call that a
		host chooses to send through the nested boundary must hash exactly as the
		in-process one, otherwise the route is consensus-visible."""
		in_process = await self._execute(
			name='route-in-process',
			line=3,
			address=ADDR_CALLER_V03,
			calldata=_calldata('call', ADDR_CALLEE_V03),
			code=None,
			is_init=False,
			resolve_hook=lambda _address, _state, _major: None,
			apply_changes=False,
		)
		nested = await self._execute(
			name='route-nested',
			line=3,
			address=ADDR_CALLER_V03,
			calldata=_calldata('call', ADDR_CALLEE_V03),
			code=None,
			is_init=False,
			resolve_hook=lambda address, _state, _major: (
				self._route(3) if address == ADDR_CALLEE_V03 else None
			),
			apply_changes=False,
		)
		assert in_process.result_kind == public_abi.ResultCode.RETURN, in_process
		assert nested.result_kind == public_abi.ResultCode.RETURN, nested
		assert in_process.result_data == nested.result_data == 303, (in_process, nested)
		# The parent's own execution hash folds the callee's small hash, which
		# commits to the outcome and to nothing about where the callee ran.
		assert in_process.execution_hash == nested.execution_hash, (
			'the route is consensus-visible',
			in_process.execution_hash.hex(),
			nested.execution_hash.hex(),
		)

	async def _assert_plain_user_error(self):
		"""A string user error is representable on both lines, so it must reach
		the caller as a user error in either direction."""
		for suffix, line, caller, callee, major in (
			('v02-to-v03', 2, ADDR_CALLER_V02, ADDR_UERR_V03, 3),
			('v03-to-v02', 3, ADDR_CALLER_V03, ADDR_UERR_V02, 2),
		):
			target = callee
			result = await self._execute(
				name=f'plain-error-{suffix}',
				line=line,
				address=caller,
				calldata=_calldata('call', callee),
				code=None,
				is_init=False,
				resolve_hook=lambda address, _state, _major, target=target, major=major: (
					self._route(major) if address == target else None
				),
				apply_changes=False,
			)
			assert result.result_kind == public_abi.ResultCode.USER_ERROR, (suffix, result)

	async def _assert_state_mode_crosses(self):
		"""The caller's state mode selects which chain view the callee reads, so
		the boundary must hand the callee exactly the mode the in-process route
		would have used."""
		for final, expected in (
			(False, public_abi.StorageType.LATEST_NON_FINAL),
			(True, public_abi.StorageType.LATEST_FINAL),
		):
			modes: dict[bool, set[public_abi.StorageType]] = {}
			for nested in (False, True):
				resolved: list[public_abi.StorageType] = []
				read_log: list[tuple[Address, public_abi.StorageType]] = []

				def resolve(address, state, _major, nested=nested, resolved=resolved):
					if address != ADDR_CALLEE_V03:
						return None
					resolved.append(state)
					return self._route(3) if nested else None

				result = await self._execute(
					name=f'state-mode-{"final" if final else "nonfinal"}-{nested}',
					line=3,
					address=ADDR_STATE_CALLER_V03,
					calldata=_calldata('call', ADDR_CALLEE_V03, final),
					code=None,
					is_init=False,
					resolve_hook=resolve,
					apply_changes=False,
					read_log=read_log,
				)
				assert result.result_kind == public_abi.ResultCode.RETURN, (
					final,
					nested,
					result,
				)
				assert result.result_data == 303, (final, nested, result)
				assert resolved == [expected], (final, nested, resolved)
				modes[nested] = {
					mode for account, mode in read_log if account == ADDR_CALLEE_V03
				}
			assert modes[False] == modes[True], (final, modes)
			assert modes[True] == {expected}, (final, modes)

	def _chain_resolve(self, address, _state, _major):
		if address == ADDR_CHAIN_V03:
			return self._route(3)
		if address == ADDR_CHAIN_V02:
			return self._route(2)
		return None

	async def _assert_deep_chain(self):
		"""Every hop of an alternating chain crosses the boundary, so the whole
		chain is a stack of nested processes. Its answer and its hash must not
		depend on which of the three runs produced them."""
		depth = 6
		leader, validator, sync = await self._lvs(
			name='deep-chain',
			line=3,
			address=ADDR_CHAIN_V03,
			calldata=_calldata('hop', depth, ADDR_CHAIN_V02),
			resolve_hook=self._chain_resolve,
		)
		# The chain alternates v0.3, v0.2, ..., so it sums 3 and 2 in turn.
		expected = sum(3 if index % 2 == 0 else 2 for index in range(depth + 1))
		for label, run in (('leader', leader), ('validator', validator), ('sync', sync)):
			assert run.result_kind == public_abi.ResultCode.RETURN, (label, run)
			assert run.result_data == expected, (label, run.result_data, expected)
		assert leader.execution_hash == validator.execution_hash, (
			leader.execution_hash.hex(),
			validator.execution_hash.hex(),
		)
		assert leader.execution_hash == sync.execution_hash, (
			leader.execution_hash.hex(),
			sync.execution_hash.hex(),
		)

	async def _assert_starved_fuel(self):
		"""The caller hands the callee whatever fuel the host says is left. A
		callee that cannot finish on it must report a canonical vm error, and two
		hosts reporting the same figure must agree on the outcome."""
		outcomes = []
		for fuel in (0, 1, 1000, 10**6):
			result = await self._execute(
				name=f'starved-fuel-{fuel}',
				line=3,
				address=ADDR_CALLER_V03,
				calldata=_calldata('call', ADDR_CALLEE_V02),
				code=None,
				is_init=False,
				resolve_hook=lambda address, _state, _major: (
					self._route(2) if address == ADDR_CALLEE_V02 else None
				),
				apply_changes=False,
				host_fuel=fuel,
			)
			assert result.result_kind != public_abi.ResultCode.INTERNAL_ERROR, (fuel, result)
			outcomes.append((fuel, result.result_kind, result.result_data))
			repeat = await self._execute(
				name=f'starved-fuel-{fuel}-again',
				line=3,
				address=ADDR_CALLER_V03,
				calldata=_calldata('call', ADDR_CALLEE_V02),
				code=None,
				is_init=False,
				resolve_hook=lambda address, _state, _major: (
					self._route(2) if address == ADDR_CALLEE_V02 else None
				),
				apply_changes=False,
				host_fuel=fuel,
			)
			assert repeat.execution_hash == result.execution_hash, (
				fuel,
				result.execution_hash.hex(),
				repeat.execution_hash.hex(),
			)
		self.notes.append(('starved fuel outcomes', [str(entry) for entry in outcomes]))

	async def _assert_child_failures_are_canonical(self):
		"""A contract must not be able to manufacture an
		internal error. Every failure a contract can pick the input for — a
		method the callee lacks, an address nothing was ever deployed to, a
		callee routed to a line its stored major disagrees with — must land on a
		user or vm error instead."""
		absent = Address('0x' + '99' * 20)
		cases = (
			# The callee has `sink`, not `answer`.
			('missing-method', 2, ADDR_CALLER_V02, ADDR_BLOB_V03, 3),
			('absent-callee', 3, ADDR_CALLER_V03, absent, 2),
			# The callee's stored major is 2; the host routes it to v0.3 anyway.
			('routed-to-wrong-line', 3, ADDR_CALLER_V03, ADDR_CALLEE_V02, 3),
		)
		observed = []
		for label, line, caller, callee, major in cases:
			target = callee
			result = await self._execute(
				name=f'canonical-{label}',
				line=line,
				address=caller,
				calldata=_calldata('call', callee),
				code=None,
				is_init=False,
				resolve_hook=lambda address, _state, _major, target=target, major=major: (
					self._route(major) if address == target else None
				),
				apply_changes=False,
			)
			assert result.result_kind != public_abi.ResultCode.INTERNAL_ERROR, (label, result)
			observed.append((label, str(result.result_kind), str(result.result_data)[:80]))
		self.notes.append(('child failure outcomes', [str(entry) for entry in observed]))

	async def _assert_resolve_loop_recursion(self):
		resolved: list[tuple[Address, int]] = []

		def resolve(address, _state, advisory_major):
			resolved.append((address, advisory_major))
			if address == ADDR_LOOP_V02:
				return self._route(2)
			if address == ADDR_LOOP_V03:
				return self._route(3)
			return None

		result = await self._execute(
			name='depth-loop',
			line=2,
			address=ADDR_LOOP_V02,
			calldata=gvm_calldata.encode({}),
			code=None,
			is_init=False,
			resolve_hook=resolve,
			timeout=30,
			# One executor process per unit of budget, so the real limit would
			# cost 511 spawns to reach; this hits the same code path in two.
			debug_initial_recursion=2,
		)
		assert result.result_kind == public_abi.ResultCode.VM_ERROR, (
			result,
			len(resolved),
			resolved[-3:],
		)
		assert result.result_data == str(public_abi.VmError.out_of().vm_recursion()), result
		assert len(resolved) == 2, len(resolved)
		# Neither line can report a meaningful major: v0.3 forwards the raw root
		# byte and v0.2 has no such byte at all, so both send the unwritten
		# default and the host decides on the address alone.
		assert resolved == [
			(ADDR_LOOP_V03, 0),
			(ADDR_LOOP_V02, 0),
		], resolved


def collect(ctx: genvm_tool.tests.stage.collection.Context) -> None:
	desc = genvm_tool.tests.test.Description(
		name='tests/system/cross-major',
		tags=frozenset({'integration', 'stable', 'cross-major'}),
		console_pool=True,
	)
	ctx.add_case(CrossMajorCase(description=desc, shared=ctx.shared))
