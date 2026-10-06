import json
import os
import pickle
import typing
from dataclasses import dataclass
from pathlib import Path

import genvm_tool.io as gvm_io
import genvm_tool.tests
import genvm_tool.tests.exec.step
import genvm_tool.tests.stage.collection
import genvm_tool.tests.test
import origin.calldata as gvm_calldata
from genvm_tool_plugins import runners
from gvm_extra.mock_host import MockHost, MockStorage, ResolveCallContractExecutorHook
from origin import base_host, host_fns, public_abi
from origin.calldata import Address
from origin.leader_public_data import LeaderPublicData

SENDER = Address('0x' + '01' * 20)
TIMESTAMP = '2024-11-26T06:42:42.424242Z'

type Fixtures = dict[str, tuple[int, Address, bytes]]


def encode_leader_public_data(output: bytes) -> bytes:
	return LeaderPublicData([output]).encode()


def encode_call(method: str, *args: typing.Any) -> bytes:
	return gvm_calldata.encode({'': method, 'args': list(args)})


def resolve_runners(code: bytes | None, root_dir: Path) -> bytes | None:
	"""
	Turns the `@RUNNER_LATEST_...@` tokens of a contract source into uids.

	Every comparison against a deployed contract's bytes has to go through this
	too, because storage holds what the executor was given.
	"""
	return None if code is None else runners.substitute(code, root_dir)


def contract_source(line: int, name: str, assets_dir: Path) -> bytes:
	"""
	Reads a contract asset written against `line`'s sdk.
	"""
	return (assets_dir / f'v0.{line}' / f'{name}.py').read_bytes()


def message(address: Address, *, is_init: bool) -> base_host.Message:
	return {
		'contract_address': address,
		'sender_address': SENDER,
		'origin_address': SENDER,
		'signer_address': SENDER,
		'chain_id': 61999,
		'value': 0,
		'is_init': is_init,
		'transaction_timestamp': TIMESTAMP,
	}


def apply_storage_deltas(
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


def assert_hashes_agree(label: str, runs: list[tuple[str, typing.Any]]) -> None:
	"""
	A disagreement is only actionable once the failure says who disagreed.
	"""
	hashes = [(name, run.execution_hash.hex()) for name, run in runs]
	assert len({digest for _name, digest in hashes}) == 1, '\n'.join(
		[f'{label}: execution hashes disagree']
		+ [f'  {name}: {digest}' for name, digest in hashes]
	)


class TestContext(base_host.Context):
	def __init__(self, logger: base_host.Logger):
		self.logger = logger
		self.stats: dict[str, typing.Any] = {}

	def on_genvm_success(self): ...
	def on_genvm_failure(self): ...

	def add_stat(self, key: str, value: typing.Any, /):
		self.stats[key] = value


@dataclass
class CrossMajorCase(genvm_tool.tests.test.Case):
	"""
	Deploys `fixtures` against `manager_service`, then calls the step's
	`method` with `arguments`.
	"""

	step_type: typing.ClassVar[type['CrossMajorStep']]

	description: genvm_tool.tests.test.Description
	shared: genvm_tool.tests.SharedContext
	manager_service: genvm_tool.tests.stage.collection.Service
	method: str
	fixtures: tuple[str, ...]
	arguments: tuple[typing.Any, ...] = ()

	async def into_steps(self) -> list[genvm_tool.tests.exec.step.Step]:
		return [self.step_type(self)]


class CrossMajorStep(genvm_tool.tests.exec.step.Python):
	"""
	Drives a real manager over the socket with a mock host.
	"""

	host_data_tx_id: typing.ClassVar[str]
	fixture_table: typing.ClassVar[Fixtures]

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
				context={'phase': self.phase, 'error': exc, 'notes': self.notes},
				elapsed_seconds=0,
			)

	async def _run_all(self):
		root = self.case.shared.root_dir
		build_info = json.loads((root / 'build' / 'info.json').read_text())
		self.versions = {
			2: build_info['executor_versions']['v0.2'],
			3: build_info['executor_versions']['v0.3'],
		}
		self.work_dir = self.case.shared.case_dir_for(self.case.description.name)
		self.storage_path = self.work_dir / 'storage.pickle'
		self.work_dir.mkdir(parents=True, exist_ok=True)
		await gvm_io.write_file_bytes(self.storage_path, pickle.dumps(MockStorage()))

		manager = self.case.manager_service.handle
		assert manager is not None
		self.manager = manager

		self.phase = 'deploy fixtures'
		for name in self.case.fixtures:
			await self._deploy(*self.fixture_table[name])
		await self._after_deploy()

		self.phase = self.case.method
		await getattr(self, self.case.method)(*self.case.arguments)

	async def _after_deploy(self) -> None:
		pass

	async def _new_host(
		self,
		name: str,
		running_address: Address,
		resolve_hook: ResolveCallContractExecutorHook | None = None,
		read_log: list[tuple[Address, public_abi.StorageView]] | None = None,
		host_fuel: int | None = None,
	) -> MockHost:
		ctx = TestContext(self.case.shared.logger)
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
			resolve_call_contract_executor_hook=resolve_hook,
		)
		if read_log is not None:
			original = host.storage_read

			async def logged(mode, address, slot, offset, le, /):
				read_log.append((Address(address), mode))
				return await original(mode, address, slot, offset, le)

			host.storage_read = logged  # type: ignore[method-assign]
		if host_fuel is not None:

			async def get_remaining_time_fee_gen_wei() -> int:
				return host_fuel

			host.get_remaining_time_fee_gen_wei = (  # type: ignore[method-assign]
				get_remaining_time_fee_gen_wei
			)
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
		resolve_hook: ResolveCallContractExecutorHook | None = None,
		timeout: float = 30,
		debug_initial_recursion: int | None = None,
		is_sync: bool = True,
		leader_public_data: bytes | None = None,
		apply_changes: bool = True,
		read_log: list[tuple[Address, public_abi.StorageView]] | None = None,
		host_fuel: int | None = None,
		hook_cross_contract_calls: bool = True,
		permissions: str | None = None,
	):
		host = await self._new_host(
			name, address, resolve_hook, read_log=read_log, host_fuel=host_fuel
		)
		request_extra: dict[str, typing.Any] = {
			'no_modules': True,
			'hook_cross_contract_calls': hook_cross_contract_calls,
		}
		if permissions is not None:
			request_extra['permissions'] = permissions
		with host as mock_host:
			try:
				async with base_host.ManagerClient(self.manager.uri) as manager_client:
					result = await base_host.run_genvm(
						mock_host,
						manager_uri=self.manager.uri,
						manager_client=manager_client,
						ctx=host.ctx,
						is_sync=is_sync,
						leader_public_data=leader_public_data,
						message=message(address, is_init=is_init),
						host_data=json.dumps(
							{'node_address': 'test', 'tx_id': self.host_data_tx_id},
							separators=(',', ':'),
						),
						host='unix://' + mock_host.path,
						code=resolve_runners(code, self.case.shared.root_dir),
						calldata=calldata,
						timeout=timeout,
						debug_mode='unsafe',
						unsafe_overrides=base_host.UnsafeOverrides(
							reroute_to=self.versions[line],
							initial_recursion=debug_initial_recursion,
						),
						request_extra=request_extra,
						bucket_totals=base_host.default_bucket_totals(line),
					)
				if apply_changes and result.result_kind == host_fns.ResultCode.RETURN:
					assert mock_host.storage is not None
					apply_storage_deltas(
						mock_host.storage,
						address,
						result.result_storage_deltas,
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
			timeout=10 * 60,
		)
		assert result.result_kind == host_fns.ResultCode.RETURN, result
		assert len(result.execution_hash) == 32, result.execution_hash

	def _route(self, line: int) -> bytes:
		"""
		Routing payload placing a callee on `line`'s executor.

		It names the directory rather than the major, because a major cannot
		pick between the live lines: every manifest key is semver major 0, so
		`{'kind': 'major'}` resolves to the newest line whatever it asks for.
		"""
		return gvm_calldata.encode({'kind': 'version', 'version': self.versions[line]})

	async def _lvs(self, *, name: str, **kwargs):
		"""
		Runs the same call as leader, validator and sync.
		"""

		async def one(suffix: str, **mode):
			return await self._execute(
				name=f'{name}-{suffix}',
				code=None,
				is_init=False,
				apply_changes=False,
				**kwargs,
				**mode,
			)

		leader = await one('leader', is_sync=False)
		validator = await one(
			'validator', is_sync=False, leader_public_data=leader.result_leader_public_data
		)
		sync = await one('sync', is_sync=True)
		return leader, validator, sync
