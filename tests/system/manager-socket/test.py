import asyncio
import contextlib
import os
import pickle
import stat
import typing
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import genvm_tool.io as gvm_io
import genvm_tool.tests
import genvm_tool.tests.stage.collection
import genvm_tool_plugins.genvm as genvm
import origin.base_host as base_host
import origin.calldata as gvm_calldata
import origin.host_fns as host_fns
from gvm_extra.mock_host import MockHost, MockStorage
from origin.calldata import Address
from origin.manager_api import CURRENT_MAJOR, Errors, Methods

HELLO_WORLD = Path(
	'executors/v0.3.x/tests/integration/hello-world/hello_world/hello_world_trivial.py'
)
BUSY_CONTRACT = Path('tests/system/permits/busy_contract.py')

FAKE_MESSAGE: base_host.Message = {
	'contract_address': Address('0x' + '00' * 20),
	'sender_address': Address('0x' + '01' * 20),
	'origin_address': Address('0x' + '01' * 20),
	'signer_address': Address('0x' + '01' * 20),
	'chain_id': 0,
	'value': 0,
	'is_init': True,
}


class _TestContext(base_host.Context):
	def __init__(self, logger: base_host.Logger):
		self.logger = logger
		self.stats: dict[str, typing.Any] = {}

	def on_genvm_success(self): ...
	def on_genvm_failure(self): ...

	def add_stat(self, key: str, value: typing.Any, /):
		self.stats[key] = value


class ManagerWsClient:
	def __init__(self, manager_uri: str, *, max_msg_size: int = 64 * 1024 * 1024):
		self.manager_uri = manager_uri
		self.max_msg_size = max_msg_size
		self.session: aiohttp.ClientSession | None = None
		self.ws: aiohttp.ClientWebSocketResponse | None = None

	async def __aenter__(self):
		if self.manager_uri.startswith('unix://'):
			connector = aiohttp.UnixConnector(path=self.manager_uri.removeprefix('unix://'))
			self.session = aiohttp.ClientSession(connector=connector)
			ws_url = 'http://localhost/ws'
		else:
			self.session = aiohttp.ClientSession()
			ws_url = self.manager_uri.rstrip('/') + '/ws'
		try:
			self.ws = await self.session.ws_connect(
				ws_url,
				max_msg_size=self.max_msg_size,
			)
		except BaseException:
			await self.close()
			raise
		return self

	async def __aexit__(self, *_args):
		await self.close()

	async def read_frame(self) -> tuple[int, int, typing.Any]:
		assert self.ws is not None
		msg = await self.ws.receive()
		if msg.type != aiohttp.WSMsgType.BINARY:
			raise ConnectionResetError(f'websocket closed: {self.ws.close_code}')
		body = bytes(msg.data)
		if len(body) < 10:
			raise ConnectionResetError('websocket message shorter than protocol header')
		method = int.from_bytes(body[:2], 'big')
		request_id = int.from_bytes(body[2:10], 'big')
		payload = gvm_calldata.decode(body[10:])
		return method, request_id, payload

	async def send(self, method: Methods, request_id: int, payload: typing.Any):
		assert self.ws is not None
		encoded = gvm_calldata.encode(payload)
		body = int(method).to_bytes(2, 'big') + request_id.to_bytes(8, 'big') + encoded
		await self.ws.send_bytes(body)

	async def send_raw_body(self, body: bytes):
		assert self.ws is not None
		await self.ws.send_bytes(body)

	async def send_oversized(self, method: Methods, request_id: int, payload_len: int):
		assert self.ws is not None
		header = int(method).to_bytes(2, 'big') + request_id.to_bytes(8, 'big')
		await self.ws.send_bytes(header + b'\x00' * payload_len)

	async def wait_closed(self) -> int | None:
		assert self.ws is not None
		close_code = None
		while not self.ws.closed:
			msg = await self.ws.receive()
			if isinstance(msg.data, int):
				close_code = msg.data
			if msg.type in (
				aiohttp.WSMsgType.CLOSE,
				aiohttp.WSMsgType.CLOSED,
				aiohttp.WSMsgType.ERROR,
			):
				break
		return self.ws.close_code or close_code

	async def close(self) -> None:
		if self.ws is not None:
			await self.ws.close()
			self.ws = None
		if self.session is not None:
			await self.session.close()
			self.session = None


class _ManagerFixture:
	def __init__(self, handle: genvm.ManagerHandle, work_dir: Path):
		self.handle = handle
		self.work_dir = work_dir
		self.work_dir.mkdir(parents=True, exist_ok=True)

	@property
	def uri(self) -> str:
		return self.handle.uri

	@property
	def socket_path(self) -> Path | None:
		return self.handle.socket_path

	async def restart(self) -> None:
		await self.handle.restart()

	async def __aenter__(self):
		return self

	async def __aexit__(self, *_args):
		pass


class _StaleSocketManagerService(genvm.ManagerService):
	async def _start_once(self) -> genvm.ManagerHandle:
		assert self._socket_path is not None
		self._socket_path.parent.mkdir(parents=True, exist_ok=True)
		self._socket_path.write_text('stale')
		return await super()._start_once()


def _run_request(
	root: Path,
	host_path: Path,
	*,
	extra_args: list[str] | None = None,
	reroute_to: str = '',
) -> dict[str, typing.Any]:
	return {
		'run': {
			'selector': {'kind': 'major', 'major': base_host.UNDEPLOYED_MAJOR},
			'message': FAKE_MESSAGE,
			'is_sync': True,
			'debug_mode': 'unsafe',
			'host_data': '{"node_address":"test","tx_id":"test"}',
			'max_execution_minutes': 20,
			'timestamp': '2024-11-26T06:42:42.424242Z',
			'host': 'unix://' + str(host_path),
			'extra_args': extra_args or [],
			'code': (root / HELLO_WORLD).read_bytes(),
			'calldata': gvm_calldata.encode({}),
			'leader_nondet_results': None,
			'bucket_totals': [2**200] * 20,
			'gas_data': base_host.DEFAULT_GAS_DATA,
			'message_fee_allocation': [],
			'initial_time_units_allocation': 60,
			'no_modules': True,
			'unsafe_overrides': base_host.UnsafeOverrides(
				reroute_to=reroute_to
			).as_request_field(),
			'deadline': '30s',
			'host_hello_data': [],
		}
	}


async def _read_hello(client: ManagerWsClient) -> int:
	method, request_id, payload = await client.read_frame()
	assert method == Methods.HELLO
	assert request_id == 0
	assert payload['hello']['protocol_major'] == CURRENT_MAJOR
	return payload['hello']['boot_id']


async def _read_error(client: ManagerWsClient, request_id: int, code: Errors):
	method, got_request_id, payload = await client.read_frame()
	assert method == Methods.ERROR, (method, got_request_id, payload)
	assert got_request_id == request_id, (method, got_request_id, payload)
	assert payload['code'] == code, payload
	return payload


async def _wait_event(client: ManagerWsClient, variant: str, timeout: float = 10.0):
	async with asyncio.timeout(timeout):
		while True:
			method, request_id, payload = await client.read_frame()
			if method == Methods.EVENT and request_id == 0 and variant in payload:
				return payload[variant]


async def _wait_terminal(client: ManagerWsClient, timeout: float = 10.0):
	"""
	Wait for whichever terminal event the run produces.

	Which one it is depends on how far the run got, and the point of the test is
	that *some* terminal event arrives without the host having to time out.
	"""
	async with asyncio.timeout(timeout):
		while True:
			method, request_id, payload = await client.read_frame()
			if method != Methods.EVENT or request_id != 0:
				continue
			for variant in ('failed_to_start', 'finished'):
				if variant in payload:
					return variant, payload[variant]


async def _terminal_from_snapshot_or_events(
	client: ManagerWsClient,
	snapshot: dict[str, typing.Any],
	timeout: float = 10.0,
):
	for variant in ('failed_to_start', 'finished'):
		if variant in snapshot:
			return variant, snapshot[variant]
	return await _wait_terminal(client, timeout=timeout)


async def _http_json(method: str, uri: str, path: str, **kwargs):
	async with aiohttp.request(method, uri + path, **kwargs) as resp:
		return resp.status, await resp.json()


async def _read_reply(
	client: ManagerWsClient,
	method: Methods,
	request_id: int,
) -> typing.Any:
	got_method, got_request_id, payload = await client.read_frame()
	assert got_method == method, (got_method, got_request_id, payload)
	assert got_request_id == request_id, (got_method, got_request_id, payload)
	return payload


async def _get_artifact(client: ManagerWsClient, genvm_id: int, field: str) -> bytes:
	offset = 0
	out = bytearray()
	request_id = 1000
	while True:
		await client.send(
			Methods.GET_ARTIFACT,
			request_id,
			{
				'get_artifact': {
					'genvm_id': genvm_id,
					'field': field,
					'offset': offset,
					'max_len': 64 * 1024,
				}
			},
		)
		artifact = await _read_reply(client, Methods.GET_ARTIFACT, request_id)
		data = bytes(artifact['data'])
		out.extend(data)
		offset += len(data)
		request_id += 1
		if offset >= artifact['total_len']:
			return bytes(out)


def _make_mock_host(
	manager: _ManagerFixture,
	name: str,
	ctx: _TestContext,
	running_address: Address | None = None,
) -> tuple[MockHost, Path]:
	tmp_dir = manager.work_dir / name
	tmp_dir.mkdir(parents=True, exist_ok=True)
	storage_path = tmp_dir / 'storage.pickle'
	with open(storage_path, 'wb') as f:
		pickle.dump(MockStorage(), f)
	host_path = (
		Path('/tmp') / f'gvm-ms-{os.getpid()}' / f'{manager.work_dir.name}-{name}.sock'
	)
	host_path.parent.mkdir(parents=True, exist_ok=True)
	host = MockHost(
		path=str(host_path),
		storage_path_pre=storage_path,
		storage_path_post=storage_path,
		balances={},
		running_address=running_address or Address('0x' + '00' * 20),
		ctx=ctx,
	)
	return host, host_path


@dataclass
class ManagerSocketCase(genvm_tool.tests.test.Case):
	description: genvm_tool.tests.test.Description
	shared: genvm_tool.tests.SharedContext
	manager_service: genvm_tool.tests.stage.collection.Service
	method: str

	async def into_steps(self) -> list[genvm_tool.tests.exec.step.Step]:
		return [ManagerSocketStep(self)]


class ManagerSocketStep(genvm_tool.tests.exec.step.Python):
	def __init__(self, case: ManagerSocketCase):
		self.case = case
		self.phase = case.method

	def to_str(self) -> str:
		return '<manager socket protocol test>'

	async def run(
		self, previous_results: list[typing.Any]
	) -> genvm_tool.tests.test.Result:
		try:
			await getattr(self, self.case.method)()
			return genvm_tool.tests.test.Result(passed=True, context={}, elapsed_seconds=0)
		except BaseException as exc:
			return genvm_tool.tests.test.Result(
				passed=False,
				context={'phase': self.phase, 'error': repr(exc)},
				elapsed_seconds=0,
			)

	async def _manager(
		self,
		name: str,
		*,
		max_message_bytes: int = 67108864,
		use_unix_listener: bool = False,
	):
		del max_message_bytes, use_unix_listener
		handle = self.case.manager_service.handle
		assert isinstance(handle, genvm.ManagerHandle)
		return _ManagerFixture(
			handle,
			self.case.shared.case_dir_for(self.case.description.name) / name,
		)

	async def _manager_with_retention(self, name: str, retention: str):
		del retention
		return await self._manager(name)

	async def _malformed_unknown_and_survival(self):
		async with await self._manager('protocol-errors') as manager:
			async with ManagerWsClient(manager.uri) as client:
				await _read_hello(client)
				unknown_body = (65535).to_bytes(2, 'big') + (1).to_bytes(8, 'big')
				await client.send_raw_body(unknown_body)
				await _read_error(client, 1, Errors.UNKNOWN_METHOD)
				await client.send(Methods.RUN, 2, b'not calldata')
				await _read_error(client, 2, Errors.MALFORMED_FRAME)
				await client.send(Methods.CANCEL, 3, {'cancel': {'genvm_id': 999}})
				await _read_error(client, 3, Errors.UNKNOWN_ID)

	async def _fatal_consumed_result_is_rejected(self):
		raw = bytes([host_fns.ResultCode.FATAL_VM_ERROR]) + gvm_calldata.encode({})
		try:
			base_host.ConsumedResult.decode(raw)
		except base_host.ConsumedResultDecodeError as exc:
			assert 'fatal_vm_error crossed the top-level result boundary' in str(exc)
		else:
			raise AssertionError('fatal consumed_result was accepted')

	async def _oversized_closes_connection(self):
		async with await self._manager('oversized', max_message_bytes=32) as manager:
			async with ManagerWsClient(manager.uri) as client:
				await _read_hello(client)
				await client.send_oversized(Methods.CANCEL, 7, payload_len=1024 * 1024)
				# The cap is enforced by tungstenite's reader, which reports it as a
				# plain read error rather than performing a close handshake, so the
				# connection drops and the client sees 1006. The property under test
				# is that the manager refuses the message instead of buffering it,
				# not which code it refuses with.
				close_code = await client.wait_closed()
				assert close_code in (
					aiohttp.WSCloseCode.ABNORMAL_CLOSURE,
					aiohttp.WSCloseCode.MESSAGE_TOO_BIG,
				), close_code

	async def _startup_failure_events_and_permits(self):
		async with await self._manager('startup-failures') as manager:
			async with ManagerWsClient(manager.uri) as client:
				await _read_hello(client)
				host_path = manager.work_dir / 'unused-host.sock'
				await client.send(
					Methods.RUN,
					10,
					_run_request(self.case.shared.root_dir, host_path, extra_args=['--bad-flag']),
				)
				method, request_id, payload = await client.read_frame()
				assert method == Methods.RUN
				assert request_id == 10
				genvm_id = payload['genvm_id']
				# The executor rejects the flag and exits on its own, so this is
				# a `finished` with its exit code rather than `failed_to_start`.
				variant, event = await _wait_terminal(client, timeout=5)
				assert event['genvm_id'] == genvm_id
				assert variant == 'finished', variant
				assert event['exit_code'] not in (0, None), event

			async with ManagerWsClient(manager.uri) as client:
				await _read_hello(client)
				status, permits_before = await _http_json('GET', manager.uri, '/permits')
				assert status == 200
				await client.send(
					Methods.RUN,
					20,
					_run_request(
						self.case.shared.root_dir,
						manager.work_dir / 'unused-host-2.sock',
						reroute_to='missing-executor-version',
					),
				)
				method, request_id, _payload = await client.read_frame()
				assert method == Methods.RUN
				assert request_id == 20
				await _wait_event(client, 'failed_to_start', timeout=5)
				for _ in range(30):
					status, permits_after = await _http_json('GET', manager.uri, '/permits')
					assert status == 200
					if permits_after['permits'] == permits_before['permits']:
						break
					await asyncio.sleep(0.1)
				else:
					raise AssertionError('permit count was not restored')

	async def _happy_path_artifact_ack_attach(self):
		async with await self._manager('happy') as manager:
			tmp_dir = manager.work_dir / 'host'
			tmp_dir.mkdir(parents=True, exist_ok=True)
			storage_path = tmp_dir / 'storage.pickle'
			await gvm_io.write_file_bytes(storage_path, pickle.dumps(MockStorage()))
			host_path = Path('/tmp') / f'gvm-ms-{os.getpid()}' / 'happy-host.sock'
			host_path.parent.mkdir(parents=True, exist_ok=True)
			ctx = _TestContext(self.case.shared.logger)
			host = MockHost(
				path=str(host_path),
				storage_path_pre=storage_path,
				storage_path_post=storage_path,
				balances={},
				running_address=Address('0x' + '00' * 20),
				ctx=ctx,
			)
			cancel_host = asyncio.Event()
			with host as mock_host:
				host_task = asyncio.create_task(
					base_host.host_loop(mock_host, cancel_host, ctx=ctx)
				)
				async with ManagerWsClient(manager.uri) as client:
					boot_id = await _read_hello(client)
					await client.send(
						Methods.RUN,
						30,
						_run_request(self.case.shared.root_dir, host_path),
					)
					method, request_id, payload = await client.read_frame()
					assert method == Methods.RUN
					assert request_id == 30
					genvm_id = payload['genvm_id']
					started = await _wait_event(client, 'started')
					assert started['genvm_id'] == genvm_id
					finished = await _wait_event(client, 'finished')
					assert finished['genvm_id'] == genvm_id
					assert finished['artifact_sizes']['stdout'] > 0
					await client.send(
						Methods.GET_ARTIFACT,
						31,
						{
							'get_artifact': {
								'genvm_id': genvm_id,
								'field': 'stdout',
								'offset': 0,
								'max_len': 64,
							}
						},
					)
					method, request_id, artifact = await client.read_frame()
					assert method == Methods.GET_ARTIFACT
					assert request_id == 31
					assert b'1' in artifact['data']
					await client.send(Methods.ACK, 32, {'ack': {'genvm_id': genvm_id}})
					method, request_id, payload = await client.read_frame()
					assert method == Methods.ACK
					assert request_id == 32
					assert payload == {}
					await client.send(
						Methods.ATTACH,
						33,
						{'attach': {'boot_id': boot_id, 'genvm_id': genvm_id}},
					)
					await _read_error(client, 33, Errors.UNKNOWN_ID)

				cancel_host.set()
				try:
					await asyncio.wait_for(host_task, timeout=5)
				except asyncio.TimeoutError:
					host_task.cancel()
					await host_task

	async def _disconnect_mid_run_reconnect_attach(self):
		async with await self._manager('reconnect-mid-run') as manager:
			ctx = _TestContext(self.case.shared.logger)
			host, host_path = _make_mock_host(manager, 'host', ctx)
			cancel_host = asyncio.Event()
			with host as mock_host:
				host_task = asyncio.create_task(
					base_host.host_loop(mock_host, cancel_host, ctx=ctx)
				)
				async with ManagerWsClient(manager.uri) as client:
					boot_id = await _read_hello(client)
					req = _run_request(self.case.shared.root_dir, host_path)
					req['run']['code'] = (self.case.shared.root_dir / BUSY_CONTRACT).read_bytes()
					req['run']['deadline'] = '1s'
					await client.send(Methods.RUN, 40, req)
					genvm_id = (await _read_reply(client, Methods.RUN, 40))['genvm_id']
					started = await _wait_event(client, 'started')
					assert started['genvm_id'] == genvm_id
					await client.close()

				async with ManagerWsClient(manager.uri) as client:
					await _read_hello(client)
					await client.send(
						Methods.ATTACH,
						41,
						{'attach': {'boot_id': boot_id, 'genvm_id': genvm_id}},
					)
					snapshot = (await _read_reply(client, Methods.ATTACH, 41))['snapshot']
					variant, terminal = await _terminal_from_snapshot_or_events(client, snapshot)
					assert variant == 'finished'
					assert terminal['genvm_id'] == genvm_id
					assert terminal['cause'] == 'deadline'
					await client.send(Methods.ACK, 42, {'ack': {'genvm_id': genvm_id}})
					await _read_reply(client, Methods.ACK, 42)
			cancel_host.set()
			with contextlib.suppress(BaseException):
				await host_task

	async def _disconnect_after_finish_before_ack(self):
		async with await self._manager('finish-before-ack') as manager:
			ctx = _TestContext(self.case.shared.logger)
			host, host_path = _make_mock_host(manager, 'host', ctx)
			cancel_host = asyncio.Event()
			with host as mock_host:
				host_task = asyncio.create_task(
					base_host.host_loop(mock_host, cancel_host, ctx=ctx)
				)
				async with ManagerWsClient(manager.uri) as client:
					boot_id = await _read_hello(client)
					await client.send(
						Methods.RUN,
						50,
						_run_request(self.case.shared.root_dir, host_path),
					)
					genvm_id = (await _read_reply(client, Methods.RUN, 50))['genvm_id']
					_finished = await _wait_event(client, 'finished')
					await client.close()

				async with ManagerWsClient(manager.uri) as client:
					await _read_hello(client)
					await client.send(
						Methods.ATTACH,
						51,
						{'attach': {'boot_id': boot_id, 'genvm_id': genvm_id}},
					)
					snapshot = (await _read_reply(client, Methods.ATTACH, 51))['snapshot']
					variant, terminal = await _terminal_from_snapshot_or_events(client, snapshot)
					assert variant == 'finished'
					assert terminal['artifact_sizes']['stdout'] > 0
					stdout = await _get_artifact(client, genvm_id, 'stdout')
					assert b'1' in stdout
					await client.send(Methods.ACK, 52, {'ack': {'genvm_id': genvm_id}})
					await _read_reply(client, Methods.ACK, 52)
			cancel_host.set()
			with contextlib.suppress(BaseException):
				await host_task

	async def _two_clients_receive_terminal(self):
		async with await self._manager('two-clients') as manager:
			ctx = _TestContext(self.case.shared.logger)
			host, host_path = _make_mock_host(manager, 'host', ctx)
			cancel_host = asyncio.Event()
			with host as mock_host:
				host_task = asyncio.create_task(
					base_host.host_loop(mock_host, cancel_host, ctx=ctx)
				)
				async with (
					ManagerWsClient(manager.uri) as first,
					ManagerWsClient(manager.uri) as second,
				):
					boot_id = await _read_hello(first)
					await _read_hello(second)
					await first.send(
						Methods.RUN,
						60,
						_run_request(self.case.shared.root_dir, host_path),
					)
					genvm_id = (await _read_reply(first, Methods.RUN, 60))['genvm_id']
					await second.send(
						Methods.ATTACH,
						61,
						{'attach': {'boot_id': boot_id, 'genvm_id': genvm_id}},
					)
					snapshot = (await _read_reply(second, Methods.ATTACH, 61))['snapshot']
					first_finished = await _wait_event(first, 'finished')
					_second_variant, second_finished = await _terminal_from_snapshot_or_events(
						second, snapshot
					)
					assert first_finished['genvm_id'] == genvm_id
					assert second_finished['genvm_id'] == genvm_id
					await first.send(Methods.ACK, 62, {'ack': {'genvm_id': genvm_id}})
					await _read_reply(first, Methods.ACK, 62)
			cancel_host.set()
			with contextlib.suppress(BaseException):
				await host_task

	async def _ack_and_ttl_drop_results(self):
		async with await self._manager_with_retention('ttl-drop', '500ms') as manager:
			ctx = _TestContext(self.case.shared.logger)
			host, host_path = _make_mock_host(manager, 'ack-host', ctx)
			cancel_host = asyncio.Event()
			with host as mock_host:
				host_task = asyncio.create_task(
					base_host.host_loop(mock_host, cancel_host, ctx=ctx)
				)
				async with ManagerWsClient(manager.uri) as client:
					boot_id = await _read_hello(client)
					await client.send(
						Methods.RUN,
						70,
						_run_request(self.case.shared.root_dir, host_path),
					)
					genvm_id = (await _read_reply(client, Methods.RUN, 70))['genvm_id']
					await _wait_event(client, 'finished')
					await client.send(Methods.ACK, 71, {'ack': {'genvm_id': genvm_id}})
					await _read_reply(client, Methods.ACK, 71)
					await client.send(
						Methods.ATTACH,
						72,
						{'attach': {'boot_id': boot_id, 'genvm_id': genvm_id}},
					)
					await _read_error(client, 72, Errors.UNKNOWN_ID)
			cancel_host.set()
			with contextlib.suppress(BaseException):
				await host_task

	async def _manager_restart_boot_mismatch(self):
		manager = await self._manager('boot-mismatch')
		async with manager:
			async with ManagerWsClient(manager.uri) as client:
				boot_id = await _read_hello(client)
				genvm_id = 1
		await manager.restart()
		async with manager:
			async with ManagerWsClient(manager.uri) as client:
				await _read_hello(client)
				await client.send(
					Methods.ATTACH,
					80,
					{'attach': {'boot_id': boot_id, 'genvm_id': genvm_id}},
				)
				await _read_error(client, 80, Errors.BOOT_ID_MISMATCH)

	async def _manager_restart_fails_waiter(self):
		manager = await self._manager('restart-run-lost')
		ctx = _TestContext(self.case.shared.logger)
		host, host_path = _make_mock_host(manager, 'host', ctx)
		cancel_host = asyncio.Event()
		with host as mock_host:
			host_task = asyncio.create_task(
				base_host.host_loop(mock_host, cancel_host, ctx=ctx)
			)
			await manager.__aenter__()
			try:
				async with base_host.ManagerClient(
					manager.uri,
					connect_timeout=ctx.get_manager_connect_timeout(),
				) as client:
					req = _run_request(self.case.shared.root_dir, host_path)['run']
					req['code'] = (self.case.shared.root_dir / BUSY_CONTRACT).read_bytes()
					state = await client.run(req)
					# The manager that owns the run dies and a fresh one takes its
					# place, handing out the same ids again -- the run cannot be
					# re-attached, so the waiter has to fail rather than reconnect.
					await manager.restart()
					try:
						terminal = await asyncio.wait_for(client.wait_terminal(state), timeout=15)
					except base_host.ManagerRunLost:
						pass
					else:
						raise AssertionError(f'run outlived its manager: {terminal}')
			finally:
				await manager.__aexit__()
			cancel_host.set()
			with contextlib.suppress(BaseException):
				await host_task

	async def _cancel_after_start_single_terminal(self):
		async with await self._manager('cancel-after-start') as manager:
			ctx = _TestContext(self.case.shared.logger)
			host, host_path = _make_mock_host(manager, 'host', ctx)
			cancel_host = asyncio.Event()
			with host as mock_host:
				host_task = asyncio.create_task(
					base_host.host_loop(mock_host, cancel_host, ctx=ctx)
				)
				async with ManagerWsClient(manager.uri) as client:
					await _read_hello(client)
					req = _run_request(self.case.shared.root_dir, host_path)
					req['run']['code'] = (self.case.shared.root_dir / BUSY_CONTRACT).read_bytes()
					req['run']['deadline'] = '30s'
					await client.send(Methods.RUN, 90, req)
					genvm_id = (await _read_reply(client, Methods.RUN, 90))['genvm_id']
					await _wait_event(client, 'started')
					await client.send(Methods.CANCEL, 91, {'cancel': {'genvm_id': genvm_id}})
					await _read_reply(client, Methods.CANCEL, 91)
					variant, terminal = await _wait_terminal(client)
					assert variant == 'finished'
					assert terminal['cause'] == 'cancelled'
					try:
						extra = await asyncio.wait_for(client.read_frame(), timeout=0.5)
					except asyncio.TimeoutError:
						extra = None
					assert extra is None or not (
						extra[0] == Methods.EVENT
						and any(k in extra[2] for k in ('failed_to_start', 'finished'))
					)
					await client.send(Methods.ACK, 92, {'ack': {'genvm_id': genvm_id}})
					await _read_reply(client, Methods.ACK, 92)
			cancel_host.set()
			with contextlib.suppress(BaseException):
				await host_task

	async def _deadline_pushes_terminal(self):
		async with await self._manager('deadline') as manager:
			ctx = _TestContext(self.case.shared.logger)
			host, host_path = _make_mock_host(manager, 'host', ctx)
			cancel_host = asyncio.Event()
			with host as mock_host:
				host_task = asyncio.create_task(
					base_host.host_loop(mock_host, cancel_host, ctx=ctx)
				)
				async with ManagerWsClient(manager.uri) as client:
					await _read_hello(client)
					req = _run_request(self.case.shared.root_dir, host_path)
					req['run']['code'] = (self.case.shared.root_dir / BUSY_CONTRACT).read_bytes()
					req['run']['deadline'] = '1s'
					await client.send(Methods.RUN, 100, req)
					genvm_id = (await _read_reply(client, Methods.RUN, 100))['genvm_id']
					variant, terminal = await _wait_terminal(client, timeout=5)
					assert variant == 'finished'
					assert terminal['cause'] == 'deadline'
					await client.send(Methods.ACK, 101, {'ack': {'genvm_id': genvm_id}})
					await _read_reply(client, Methods.ACK, 101)
			cancel_host.set()
			with contextlib.suppress(BaseException):
				await host_task

	async def _client_never_returns_ttl_drains(self):
		async with await self._manager_with_retention('ttl-drain', '500ms') as manager:
			ctx = _TestContext(self.case.shared.logger)
			host, host_path = _make_mock_host(manager, 'host', ctx)
			cancel_host = asyncio.Event()
			with host as mock_host:
				host_task = asyncio.create_task(
					base_host.host_loop(mock_host, cancel_host, ctx=ctx)
				)
				async with ManagerWsClient(manager.uri) as client:
					boot_id = await _read_hello(client)
					await client.send(
						Methods.RUN,
						110,
						_run_request(self.case.shared.root_dir, host_path),
					)
					genvm_id = (await _read_reply(client, Methods.RUN, 110))['genvm_id']
				with contextlib.suppress(BaseException):
					await host_task
				for _ in range(330):
					status, body = await _http_json('GET', manager.uri, '/status')
					assert status == 200
					if str(genvm_id) not in body['executions']:
						break
					await asyncio.sleep(0.2)
				else:
					raise AssertionError('retained execution did not drain after TTL')
				async with ManagerWsClient(manager.uri) as client:
					await _read_hello(client)
					await client.send(
						Methods.ATTACH,
						111,
						{'attach': {'boot_id': boot_id, 'genvm_id': genvm_id}},
					)
					await _read_error(client, 111, Errors.UNKNOWN_ID)

	async def _http_and_socket_same_fixture(self):
		async with await self._manager('http-socket-parity') as manager:
			http_res = await self._run_fixture_over_http(manager, 'http')
			socket_res = await self._run_fixture_over_socket(manager, 'socket')
			assert http_res['stdout'] == socket_res.stdout
			assert http_res['stderr'] == socket_res.stderr
			assert http_res['result_kind'] == socket_res.result_kind
			assert http_res['result_data'] == socket_res.result_data

	async def _run_fixture_over_http(self, manager: _ManagerFixture, name: str):
		ctx = _TestContext(self.case.shared.logger)
		host, host_path = _make_mock_host(manager, name, ctx)
		cancel_host = asyncio.Event()
		with host as mock_host:
			host_task = asyncio.create_task(
				base_host.host_loop(mock_host, cancel_host, ctx=ctx)
			)
			req = _run_request(self.case.shared.root_dir, host_path)['run']
			async with aiohttp.request(
				'POST',
				f'{manager.uri}/genvm/run',
				data=gvm_calldata.encode(req),
			) as resp:
				body = await resp.json()
				assert resp.status == 200, body
				genvm_id = body['id']
			with contextlib.suppress(BaseException):
				await host_task
			for _ in range(30):
				status, body = await _http_json('GET', manager.uri, f'/genvm/{genvm_id}')
				assert status == 200
				if body['status'] is not None:
					result = body['status']
					break
				await asyncio.sleep(0.2)
			else:
				raise AssertionError('HTTP shim result did not become available')
		assert isinstance(result['consumed_result'], str)
		consumed = base_host.ConsumedResult.decode(result.get('consumed_result'))
		return {
			'stdout': result['stdout'],
			'stderr': result['stderr'],
			'result_kind': consumed.result_kind,
			'result_data': consumed.result_data,
		}

	async def _run_fixture_over_socket(self, manager: _ManagerFixture, name: str):
		ctx = _TestContext(self.case.shared.logger)
		host, host_path = _make_mock_host(manager, name, ctx)
		with host as mock_host:
			async with base_host.ManagerClient(
				manager.uri,
				connect_timeout=ctx.get_manager_connect_timeout(),
			) as manager_client:
				return await base_host.run_genvm(
					mock_host,
					manager_uri=manager.uri,
					manager_client=manager_client,
					ctx=ctx,
					is_sync=True,
					message=FAKE_MESSAGE,
					host_data='{"node_address":"test","tx_id":"test"}',
					host='unix://' + str(host_path),
					code=(self.case.shared.root_dir / HELLO_WORLD).read_bytes(),
					calldata=gvm_calldata.encode({}),
					timeout=30,
					debug_mode='unsafe',
					request_extra={'no_modules': True},
					bucket_totals=[2**200] * 20,
				)

	async def _unix_parent_and_stale_socket(self):
		async with await self._manager('unix-stale', use_unix_listener=True) as manager:
			assert manager.socket_path is not None
			socket_path = manager.socket_path
			mode = os.stat(socket_path).st_mode
			assert stat.S_ISSOCK(mode)


def collect(
	ctx: genvm_tool.tests.stage.collection.Context,
	*,
	manager_semaphore: genvm_tool.tests.stage.collection.Semaphore,
	build_dir: Path,
	ci: bool,
) -> None:
	service_root = ctx.shared.artifacts_dir / 'manager_socket' / 'services'
	base_config = {
		'permits': {'total': 2},
		'execution_retention': '30s',
	}

	def service(
		name: str,
		*,
		config: dict[str, typing.Any] | None = None,
		socket_path: Path | None = None,
		implementation: type[genvm.ManagerService] = genvm.ManagerService,
	):
		merged_config = {**base_config, **(config or {})}
		return ctx.new_service(
			name=f'manager-socket-{name}',
			manager=implementation(
				bin_path=build_dir / 'out' / 'bin' / 'genvm-modules',
				log_path=service_root / name / 'manager.log',
				env=ctx.configuration,
				ci=ci,
				config=merged_config,
				socket_path=socket_path,
			),
			semaphores=[manager_semaphore],
		)

	cases = [
		(
			'fatal-result-rejected',
			'_fatal_consumed_result_is_rejected',
			{},
			None,
			genvm.ManagerService,
		),
		(
			'protocol-errors',
			'_malformed_unknown_and_survival',
			{},
			None,
			genvm.ManagerService,
		),
		(
			'oversized-close',
			'_oversized_closes_connection',
			{'max_message_bytes': 32},
			None,
			genvm.ManagerService,
		),
		(
			'startup-failures',
			'_startup_failure_events_and_permits',
			{},
			None,
			genvm.ManagerService,
		),
		('happy-path', '_happy_path_artifact_ack_attach', {}, None, genvm.ManagerService),
		(
			'reconnect-mid-run',
			'_disconnect_mid_run_reconnect_attach',
			{},
			None,
			genvm.ManagerService,
		),
		(
			'finish-before-ack',
			'_disconnect_after_finish_before_ack',
			{},
			None,
			genvm.ManagerService,
		),
		(
			'multiple-attachments',
			'_two_clients_receive_terminal',
			{},
			None,
			genvm.ManagerService,
		),
		(
			'ack-drops-result',
			'_ack_and_ttl_drop_results',
			{'execution_retention': '500ms'},
			None,
			genvm.ManagerService,
		),
		('boot-mismatch', '_manager_restart_boot_mismatch', {}, None, genvm.ManagerService),
		(
			'restart-loses-run',
			'_manager_restart_fails_waiter',
			{},
			None,
			genvm.ManagerService,
		),
		('cancel', '_cancel_after_start_single_terminal', {}, None, genvm.ManagerService),
		('deadline', '_deadline_pushes_terminal', {}, None, genvm.ManagerService),
		(
			'retention-drain',
			'_client_never_returns_ttl_drains',
			{'execution_retention': '500ms'},
			None,
			genvm.ManagerService,
		),
		('http-parity', '_http_and_socket_same_fixture', {}, None, genvm.ManagerService),
		(
			'unix-stale-socket',
			'_unix_parent_and_stale_socket',
			{},
			Path('/tmp') / f'gvm-ms-{os.getpid()}' / 'unix-stale.sock',
			_StaleSocketManagerService,
		),
	]
	for slug, method, config, socket_path, implementation in cases:
		manager_service = service(
			slug,
			config=config,
			socket_path=socket_path,
			implementation=implementation,
		)
		desc = genvm_tool.tests.test.Description(
			name=f'tests/system/manager-socket/{slug}',
			needed_services=frozenset({manager_service}),
			tags=frozenset({'integration', 'stable', 'feature-manager-socket'}),
		)
		ctx.add_case(
			ManagerSocketCase(
				description=desc,
				shared=ctx.shared,
				manager_service=manager_service,
				method=method,
			)
		)
