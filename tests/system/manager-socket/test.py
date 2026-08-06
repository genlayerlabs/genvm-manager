import asyncio
import contextlib
import json
import os
import pickle
import socket
import stat
import typing
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import genvm_tool.tests
import genvm_tool.tests.stage.collection
import origin.base_host as base_host
import origin.calldata as gvm_calldata
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


class ManagerProc:
	def __init__(
		self,
		*,
		root: Path,
		build_dir: Path,
		artifacts_dir: Path,
		name: str,
		socket_path: Path,
		max_message_bytes: int = 67108864,
		execution_retention: str = '30s',
		use_unix_listener: bool = False,
	):
		self.root = root
		self.build_dir = build_dir
		self.work_dir = artifacts_dir / name
		self.socket_path = socket_path
		self.max_message_bytes = max_message_bytes
		self.execution_retention = execution_retention
		self.use_unix_listener = use_unix_listener
		self.process: asyncio.subprocess.Process | None = None
		self.port = self._unused_port()
		self.log_file = None

	@staticmethod
	def _unused_port() -> int:
		with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
			sock.bind(('127.0.0.1', 0))
			return sock.getsockname()[1]

	@property
	def uri(self) -> str:
		if self.use_unix_listener:
			return f'unix://{self.socket_path}'
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
					f'execution_retention: {self.execution_retention}',
					f'max_message_bytes: {self.max_message_bytes}',
					'',
				]
			)
		)
		listener_args = (
			['--socket', str(self.socket_path)]
			if self.use_unix_listener
			else ['--port', str(self.port)]
		)
		self.log_file = open(self.work_dir / 'manager.log', 'wb')
		self.process = await asyncio.subprocess.create_subprocess_exec(
			str(self.build_dir / 'out' / 'bin' / 'genvm-modules'),
			'manager',
			*listener_args,
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
				if self.use_unix_listener:
					async with aiohttp.ClientSession(
						connector=aiohttp.UnixConnector(path=str(self.socket_path))
					) as session:
						async with session.get(
							'http://localhost/status',
							timeout=aiohttp.ClientTimeout(total=1),
						) as resp:
							if resp.status == 200:
								return
				else:
					async with aiohttp.request(
						'GET',
						f'{self.uri}/status',
						timeout=aiohttp.ClientTimeout(total=1),
					) as resp:
						if resp.status == 200:
							return
			except BaseException as exc:
				last_error = exc
			await asyncio.sleep(0.1)
		raise TimeoutError(f'manager did not become healthy: {last_error}')


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
	"""Wait for whichever terminal event the run produces.

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
	manager: ManagerProc,
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

	async def into_steps(self) -> list[genvm_tool.tests.exec.step.Step]:
		return [ManagerSocketStep(self)]


class ManagerSocketStep(genvm_tool.tests.exec.step.Python):
	def __init__(self, case: ManagerSocketCase):
		self.case = case
		self.phase = 'start'

	def to_str(self) -> str:
		return '<manager socket protocol test>'

	async def run(
		self, previous_results: list[typing.Any]
	) -> genvm_tool.tests.test.Result:
		try:
			await self._run_all()
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
		artifacts = self.case.shared.artifacts_dir / 'manager_socket'
		socket_path = Path('/tmp') / f'gvm-ms-{os.getpid()}' / f'{name}.sock'
		return ManagerProc(
			root=self.case.shared.root_dir,
			build_dir=Path(
				json.loads((self.case.shared.root_dir / 'build' / 'info.json').read_text())[
					'build_dir'
				]
			),
			artifacts_dir=artifacts,
			name=name,
			socket_path=socket_path,
			max_message_bytes=max_message_bytes,
			use_unix_listener=use_unix_listener,
		)

	async def _manager_with_retention(self, name: str, retention: str):
		artifacts = self.case.shared.artifacts_dir / 'manager_socket'
		socket_path = Path('/tmp') / f'gvm-ms-{os.getpid()}' / f'{name}.sock'
		return ManagerProc(
			root=self.case.shared.root_dir,
			build_dir=Path(
				json.loads((self.case.shared.root_dir / 'build' / 'info.json').read_text())[
					'build_dir'
				]
			),
			artifacts_dir=artifacts,
			name=name,
			socket_path=socket_path,
			execution_retention=retention,
		)

	async def _run_all(self):
		self.phase = 'protocol errors'
		await self._malformed_unknown_and_survival()
		self.phase = 'oversized close'
		await self._oversized_closes_connection()
		self.phase = 'startup failures'
		await self._startup_failure_events_and_permits()
		self.phase = 'happy path'
		await self._happy_path_artifact_ack_attach()
		self.phase = 'reconnect mid-run'
		await self._disconnect_mid_run_reconnect_attach()
		self.phase = 'finish before ack reconnect'
		await self._disconnect_after_finish_before_ack()
		self.phase = 'multi attach'
		await self._two_clients_receive_terminal()
		self.phase = 'ack and ttl'
		await self._ack_and_ttl_drop_results()
		self.phase = 'boot mismatch'
		await self._manager_restart_boot_mismatch()
		self.phase = 'restart loses run'
		await self._manager_restart_fails_waiter()
		self.phase = 'cancel after start'
		await self._cancel_after_start_single_terminal()
		self.phase = 'deadline event'
		await self._deadline_pushes_terminal()
		self.phase = 'ttl drain'
		await self._client_never_returns_ttl_drains()
		self.phase = 'http socket parity'
		await self._http_and_socket_same_fixture()
		self.phase = 'unix stale'
		await self._unix_parent_and_stale_socket()

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
			with open(storage_path, 'wb') as f:
				pickle.dump(MockStorage(), f)
			host_path = tmp_dir / 'host.sock'
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
					await manager.__aexit__()
					await manager.__aenter__()
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

	async def _run_fixture_over_http(self, manager: ManagerProc, name: str):
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

	async def _run_fixture_over_socket(self, manager: ManagerProc, name: str):
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
		artifacts = Path('/tmp') / f'gvm-ms-{os.getpid()}'
		socket_path = artifacts / 'unix-stale.sock'
		socket_path.parent.mkdir(parents=True, exist_ok=True)
		socket_path.write_text('stale')
		async with await self._manager('unix-stale', use_unix_listener=True) as manager:
			assert manager.socket_path == socket_path
			mode = os.stat(socket_path).st_mode
			assert stat.S_ISSOCK(mode)


def collect(ctx: genvm_tool.tests.stage.collection.Context) -> None:
	# console_pool: this case starts, restarts and kills managers of its own, and
	# waits out retention sweeps. Sharing the runner with other cases makes their
	# manager unreachable partway through, which they report as a connection
	# refused far away from the cause.
	desc = genvm_tool.tests.test.Description(
		name='tests/system/manager-socket',
		tags=frozenset({'integration', 'stable', 'manager-socket'}),
		console_pool=True,
	)
	ctx.add_case(ManagerSocketCase(description=desc, shared=ctx.shared))
