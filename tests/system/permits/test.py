"""
Permits semaphore system test.

Tests that the manager's permit semaphore correctly blocks new genvm executions
when all permits are exhausted. Evaluated by ``collection.Context.collect_dir``;
``collect`` registers the single case (it needs the manager service handle).
"""

import asyncio
import pickle
import typing
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import genvm_tool.io as gvm_io
import genvm_tool.tests
import genvm_tool.tests.stage.collection
import origin.base_host as base_host
import origin.calldata as gvm_calldata
from gvm_extra.mock_host import MockHost, MockStorage
from origin.calldata import Address

# Fixture lives next to this test definition.
BUSY_CONTRACT = Path(__file__).resolve().parent / 'busy_contract.py'

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
	"""Minimal Context for run_genvm calls."""

	def __init__(self, logger: base_host.Logger):
		self.logger = logger

	def on_genvm_success(self): ...
	def on_genvm_failure(self): ...
	def add_stat(self, key: str, value: typing.Any, /): ...


@dataclass
class PermitsTestCase(genvm_tool.tests.test.Case):
	description: genvm_tool.tests.test.Description
	manager_service: genvm_tool.tests.stage.collection.Service
	shared: genvm_tool.tests.SharedContext
	method: str

	async def into_steps(self) -> list[genvm_tool.tests.exec.step.Step]:
		return [PermitsTestStep(self)]


class PermitsTestStep(genvm_tool.tests.exec.step.Python):
	def __init__(self, case: PermitsTestCase):
		self._case = case

	def to_str(self) -> str:
		return '<permits semaphore test>'

	async def run(
		self, previous_results: list[typing.Any]
	) -> genvm_tool.tests.test.Result:
		port = self._case.manager_service.meta['port']
		self._reroute_to = self._case.manager_service.meta.get('reroute_to', '')
		manager_uri = f'http://localhost:{port}'
		shared = self._case.shared
		logger = shared.logger

		# the manager refuses to go below the most expensive run, so take that
		# many permits and fill them with sync runs exactly
		costs = (await self._get_status(manager_uri))['permits']
		sync_cost = costs['per_sync_run']
		min_permits = max(sync_cost, costs['per_nondet_run'])
		N = -(-min_permits // sync_cost)
		num_permits = N * sync_cost

		original_permits = await self._get_permits(manager_uri)
		genvm_ids: list[str] = []
		bg_tasks: list[asyncio.Task] = []

		shut_downer = asyncio.Event()
		async with base_host.ManagerClient(manager_uri) as manager_client:
			try:
				await self._set_permits(manager_uri, num_permits)
				logger.debug('permits set', permits=num_permits)

				# Start N background genvms, each sync
				tmp_dir = shared.artifacts_dir / 'permits_test'
				tmp_dir.mkdir(parents=True, exist_ok=True)

				code = BUSY_CONTRACT.read_bytes()

				for i in range(N):
					sock_path = f'/tmp/genvm-test-permits-{i}.sock'
					storage_path = tmp_dir / f'storage_{i}.pickle'

					# Create empty storage
					await gvm_io.write_file_bytes(storage_path, pickle.dumps(MockStorage()))

					ctx = _TestContext(logger)
					host = MockHost(
						path=sock_path,
						storage_path_pre=storage_path,
						storage_path_post=storage_path,
						balances={},
						running_address=Address('0x' + '00' * 20),
						ctx=ctx,
					)

					task = asyncio.create_task(
						self._run_background_genvm(
							host,
							manager_uri,
							manager_client,
							ctx,
							code,
							sock_path,
							genvm_ids,
							shut_downer,
						)
					)
					bg_tasks.append(task)

				# Wait for permits to be consumed
				for _ in range(30):
					status = await self._get_status(manager_uri)
					current = status['permits']['current']
					logger.trace('polling permits', current=current)
					if current == 0:
						break
					await asyncio.sleep(0.5)
				else:
					return genvm_tool.tests.test.Result(
						passed=False,
						context={'reason': 'permits were not consumed within timeout'},
						elapsed_seconds=0,
					)

				logger.debug('all permits consumed, testing blocking behavior')

				# Try to start another genvm; it should block on semaphore.
				if self._case.method == 'http-blocks':
					passed = await self._assert_blocks(manager_uri, code)
					reason = 'POST /genvm/run did not block when permits exhausted'
				elif self._case.method == 'socket-cancel':
					passed = await self._assert_socket_cancel_queued(
						manager_uri, manager_client, code
					)
					reason = (
						'socket cancel while queued consumed a permit or missed terminal event'
					)
				else:
					raise ValueError(f'unknown permits test method: {self._case.method}')
				if not passed:
					return genvm_tool.tests.test.Result(
						passed=False,
						context={'reason': reason},
						elapsed_seconds=0,
					)

				logger.info('permits test passed')
				return genvm_tool.tests.test.Result(passed=True, context={}, elapsed_seconds=0)

			finally:
				shut_downer.set()
				# Shut down running genvms
				for gid in genvm_ids:
					try:
						async with aiohttp.request(
							'DELETE',
							f'{manager_uri}/genvm/{gid}',
							timeout=aiohttp.ClientTimeout(total=10),
						):
							pass
					except Exception as e:
						logger.warning('failed to delete genvm', genvm_id=gid, error=e)
						pass

				for task in bg_tasks:
					try:
						await task
					except (asyncio.CancelledError, Exception):
						pass

				# Restore permits
				await self._set_permits(manager_uri, original_permits)
				logger.debug('permits restored', permits=original_permits)

	async def _run_background_genvm(
		self,
		host: MockHost,
		manager_uri: str,
		manager_client: base_host.ManagerClient,
		ctx: _TestContext,
		code: bytes,
		sock_path: str,
		genvm_ids: list[str],
		shut_downer: asyncio.Event | None = None,
	) -> None:
		with host as mock_host:
			# We wrap run_genvm but intercept the genvm_id.
			# run_genvm blocks until completion; we rely on task cancellation to stop it.
			try:
				await base_host.run_genvm(
					mock_host,
					manager_uri=manager_uri,
					manager_client=manager_client,
					ctx=ctx,
					is_sync=True,
					message=FAKE_MESSAGE,
					host_data='{"node_address":"test","tx_id":"test"}',
					host='unix://' + sock_path,
					code=code,
					calldata=gvm_calldata.encode({}),
					timeout=300,
					debug_mode='unsafe',
					unsafe_overrides=base_host.UnsafeOverrides(
						reroute_to=getattr(self, '_reroute_to', ''),
					),
					request_extra={'no_modules': True},
					shutdown_early=shut_downer,
					bucket_totals=[2**200] * 20,
				)
			except Exception:
				pass  # expected when cancelled or contract crashes

	async def _assert_blocks(self, manager_uri: str, code: bytes) -> bool:
		"""Make a raw POST /genvm/run and assert it blocks (times out)."""
		data = gvm_calldata.encode(
			{
				'selector': {'kind': 'major', 'major': base_host.UNDEPLOYED_MAJOR},
				'message': FAKE_MESSAGE,
				'is_sync': True,
				'host_data': '{"node_address":"test","tx_id":"test"}',
				'max_execution_minutes': 20,
				'timestamp': '2024-11-26T06:42:42.424242Z',
				'host': 'unix:///tmp/genvm-test-permits-block.sock',
				'extra_args': [],
				'code': code,
				'calldata': gvm_calldata.encode({}),
				'bucket_totals': [2**200] * 20,
				'no_modules': True,
				'leader_public_data': b'',
				'initial_time_units_allocation': 60,
			}
		)

		try:
			async with asyncio.timeout(5):
				async with aiohttp.request(
					'POST',
					f'{manager_uri}/genvm/run',
					data=data,
					timeout=aiohttp.ClientTimeout(total=10),
				) as resp:
					# If we get here, the request didn't block
					body = await resp.json()
					# Clean up the accidentally started genvm
					if 'id' in body:
						try:
							async with aiohttp.request(
								'DELETE',
								f'{manager_uri}/genvm/{body["id"]}',
								timeout=aiohttp.ClientTimeout(total=5),
							):
								pass
						except Exception:
							pass
					return False
		except (asyncio.TimeoutError, TimeoutError):
			return True

	async def _assert_socket_cancel_queued(
		self,
		manager_uri: str,
		client: base_host.ManagerClient,
		code: bytes,
	) -> bool:
		payload = {
			'selector': {'kind': 'major', 'major': base_host.UNDEPLOYED_MAJOR},
			'message': FAKE_MESSAGE,
			'is_sync': True,
			'debug_mode': 'unsafe',
			'host_data': '{"node_address":"test","tx_id":"test"}',
			'max_execution_minutes': 20,
			'timestamp': '2024-11-26T06:42:42.424242Z',
			'host': 'unix:///tmp/genvm-test-permits-socket-cancel.sock',
			'extra_args': [],
			'code': code,
			'calldata': gvm_calldata.encode({}),
			'bucket_totals': [2**200] * 20,
			'no_modules': True,
			'leader_public_data': b'',
			'initial_time_units_allocation': 60,
			'unsafe_overrides': base_host.UnsafeOverrides(
				reroute_to=self._reroute_to,
			).as_request_field(),
			'deadline': '30s',
		}
		state = await client.run(payload)
		before = await self._get_status(manager_uri)
		if before['permits']['current'] != 0:
			return False
		await client.cancel(state.boot_id, state.genvm_id)
		terminal = await client.wait_terminal(state)
		after = await self._get_status(manager_uri)
		await client.ack(state.boot_id, state.genvm_id)
		if 'finished' not in terminal:
			return False
		finished = terminal['finished']
		return (
			finished['cause'] == 'cancelled'
			and finished['exit_code'] is None
			and after['permits']['current'] == 0
		)

	async def _get_permits(self, manager_uri: str) -> int:
		async with aiohttp.request('GET', f'{manager_uri}/permits') as resp:
			data = await resp.json()
			return data['permits']

	async def _set_permits(self, manager_uri: str, permits: int) -> None:
		async with aiohttp.request(
			'POST',
			f'{manager_uri}/permits',
			json={'permits': permits},
		) as resp:
			if resp.status != 200:
				raise RuntimeError(f'set_permits failed: {resp.status}')

	async def _get_status(self, manager_uri: str) -> dict:
		async with aiohttp.request('GET', f'{manager_uri}/status') as resp:
			return await resp.json()


def collect(
	ctx: genvm_tool.tests.stage.collection.Context,
	*,
	manager_service: genvm_tool.tests.stage.collection.Service,
) -> None:
	for slug in ('http-blocks', 'socket-cancel'):
		desc = genvm_tool.tests.test.Description(
			name=f'tests/system/permits/{slug}',
			needed_services=frozenset({manager_service}),
			tags=frozenset({'integration', 'stable', 'feature-permit'}),
			console_pool=True,
		)
		ctx.add_case(
			PermitsTestCase(
				description=desc,
				manager_service=manager_service,
				shared=ctx.shared,
				method=slug,
			)
		)
