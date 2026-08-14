"""
Cross-major CallContract observability and determinism integration tests.

This case reuses the real-manager harness from ``tests/system/cross-major``
without adding more concerns to that already-large case.
"""

import functools
import importlib.util
import json
import pickle
import sys
import typing
from dataclasses import dataclass
from pathlib import Path

import genvm_tool.io as gvm_io
import genvm_tool.tests
import genvm_tool.tests.stage.collection
from gvm_extra.mock_host import MockStorage
from origin import host_fns, public_abi
from origin.calldata import Address


def _load_cross_major_harness():
	path = Path(__file__).parents[1] / 'cross-major' / 'test.py'
	spec = importlib.util.spec_from_file_location('cross_major_system_harness', path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


base = _load_cross_major_harness()

ADDR_CALLER_V02 = Address('0x' + '41' * 20)
ADDR_OBSERVER_V03 = Address('0x' + '42' * 20)
ADDR_HELPER_V03 = Address('0x' + '43' * 20)
ADDR_SELF_V03 = Address('0x' + '44' * 20)
ADDR_TRAP_V03 = Address('0x' + '45' * 20)
ADDR_USER_ERROR_V03 = Address('0x' + '46' * 20)
ADDR_CHAIN_V03 = Address('0x' + '47' * 20)
ADDR_CHAIN_V02 = Address('0x' + '48' * 20)
ABSENT = Address('0x' + '99' * 20)

# Mirrors `CROSS_MAJOR_RECURSION` in implementation/src/manager/run.rs: how many
# delegated hops one chain of contract calls may make.
CROSS_MAJOR_RECURSION = 6


_contract_source = functools.partial(
	base._contract_source,
	assets_dir=Path(__file__).resolve().parent / 'assets',
)

CALLER_V02 = _contract_source(2, 'caller')
OBSERVER_V03 = _contract_source(3, 'observer')
HELPER_V03 = _contract_source(3, 'helper')
SELF_V03 = _contract_source(3, 'self_recursion')
TRAP_V03 = _contract_source(3, 'trap')
USER_ERROR_V03 = _contract_source(3, 'user_error')
CHAIN_V03 = _contract_source(3, 'chain')
CHAIN_V02 = _contract_source(2, 'chain')

FIXTURES = {
	'caller-v02': (2, ADDR_CALLER_V02, CALLER_V02),
	'observer-v03': (3, ADDR_OBSERVER_V03, OBSERVER_V03),
	'helper-v03': (3, ADDR_HELPER_V03, HELPER_V03),
	'self-v03': (3, ADDR_SELF_V03, SELF_V03),
	'trap-v03': (3, ADDR_TRAP_V03, TRAP_V03),
	'user-error-v03': (3, ADDR_USER_ERROR_V03, USER_ERROR_V03),
	'chain-v03': (3, ADDR_CHAIN_V03, CHAIN_V03),
	'chain-v02': (2, ADDR_CHAIN_V02, CHAIN_V02),
}


@dataclass
class ObservabilityCase(genvm_tool.tests.test.Case):
	description: genvm_tool.tests.test.Description
	shared: genvm_tool.tests.SharedContext
	manager_service: genvm_tool.tests.stage.collection.Service
	method: str
	fixtures: tuple[str, ...]
	arguments: tuple[typing.Any, ...] = ()

	async def into_steps(self) -> list[genvm_tool.tests.exec.step.Step]:
		return [ObservabilityStep(self)]


class ObservabilityStep(base.CrossMajorStep):
	async def _run_all(self):
		root = self.case.shared.root_dir
		build_info = json.loads((root / 'build' / 'info.json').read_text())
		self.build_dir = Path(build_info['build_dir'])
		self.versions = {
			2: build_info['executor_versions']['v0.2'],
			3: build_info['executor_versions']['v0.3'],
		}
		work_dir = self.case.shared.case_dir_for(self.case.description.name)
		self.storage_path = work_dir / 'storage.pickle'
		work_dir.mkdir(parents=True, exist_ok=True)
		await gvm_io.write_file_bytes(self.storage_path, pickle.dumps(MockStorage()))

		manager = self.case.manager_service.handle
		assert manager is not None
		self.manager = manager

		self.phase = 'deploy observability fixtures'
		for line, address, code in (FIXTURES[name] for name in self.case.fixtures):
			await self._deploy(line, address, code)

		self.phase = self.case.method
		await getattr(self, self.case.method)(*self.case.arguments)

	async def _execute(
		self,
		*,
		permissions: str = 'wscn',
		**kwargs,
	):
		name = typing.cast(str, kwargs['name'])
		address = typing.cast(Address, kwargs['address'])
		resolve_hook = kwargs.get('resolve_hook')
		read_log = kwargs.get('read_log')
		host_fuel = kwargs.get('host_fuel')
		host = await self._new_host(
			name,
			address,
			resolve_hook,
			read_log=read_log,
			host_fuel=host_fuel,
		)
		ctx = host.ctx
		with host as mock_host:
			try:
				async with base.base_host.ManagerClient(self.manager.uri) as manager_client:
					result = await base.base_host.run_genvm(
						mock_host,
						manager_uri=self.manager.uri,
						manager_client=manager_client,
						ctx=ctx,
						is_sync=kwargs.get('is_sync', True),
						leader_nondet_results=kwargs.get('leader_nondet_results'),
						message=base._message(
							address, is_init=typing.cast(bool, kwargs['is_init'])
						),
						host_data='{"node_address":"test","tx_id":"cross-major-observe"}',
						host='unix://' + mock_host.path,
						code=base.resolve_runners(
							typing.cast('bytes | None', kwargs.get('code')),
							self.case.shared.root_dir,
						),
						calldata=typing.cast(bytes, kwargs['calldata']),
						timeout=kwargs.get('timeout', 30),
						debug_mode='unsafe',
						unsafe_overrides=base.base_host.UnsafeOverrides(
							reroute_to=self.versions[typing.cast(int, kwargs['line'])],
							initial_recursion=kwargs.get('debug_initial_recursion'),
						),
						request_extra={
							'permissions': permissions,
							'no_modules': True,
							'hook_cross_contract_calls': kwargs.get(
								'hook_cross_contract_calls', True
							),
						},
						bucket_totals=[2**200] * 20,
					)
				if (
					kwargs.get('apply_changes', True)
					and result.result_kind == host_fns.ResultCode.RETURN
				):
					assert mock_host.storage is not None
					base._apply_storage_changes(
						mock_host.storage,
						address,
						result.result_storage_changes,
					)
				return result
			finally:
				await host.stop_connections()

	async def _lvs_extended(self, *, name: str, **kwargs):
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
			'validator',
			is_sync=False,
			leader_nondet_results=leader.result_nondet_results,
		)
		sync = await one('sync', is_sync=True)
		assert leader.execution_hash == validator.execution_hash, (
			name,
			leader.execution_hash.hex(),
			validator.execution_hash.hex(),
		)
		assert leader.execution_hash == sync.execution_hash, (
			name,
			leader.execution_hash.hex(),
			sync.execution_hash.hex(),
		)
		for label, result in (
			('leader', leader),
			('validator', validator),
			('sync', sync),
		):
			assert result.result_kind != host_fns.ResultCode.INTERNAL_ERROR, (
				name,
				label,
				result,
			)
		return leader, validator, sync

	def _route_v03(self, address, _state, _major):
		if address in {
			ADDR_OBSERVER_V03,
			ADDR_SELF_V03,
			ADDR_TRAP_V03,
			ADDR_USER_ERROR_V03,
		}:
			return self._route(3)
		return None

	def _route_chain(self, address, _state, _major):
		if address == ADDR_CHAIN_V03:
			return self._route(3)
		if address == ADDR_CHAIN_V02:
			return self._route(2)
		return None

	async def _assert_validator_mode(self):
		# A validator presented with a leader result that this deterministic call
		# never consumes must reject it. This prevents l/v agreement below from
		# being vacuous because both requests accidentally ran as sync.
		bogus = await self._execute(
			name='validator-mode-is-real',
			line=3,
			address=ADDR_CHAIN_V03,
			calldata=base._calldata('hop', 1, ADDR_CHAIN_V02),
			code=None,
			is_init=False,
			resolve_hook=self._route_chain,
			apply_changes=False,
			is_sync=False,
			leader_nondet_results=[base.gvm_calldata.encode({})],
		)
		assert bogus.result_kind == host_fns.ResultCode.VM_ERROR, bogus

	async def _assert_context(self):
		routes: list[Address] = []

		def resolve(address, state, major):
			routes.append(address)
			return self._route_v03(address, state, major)

		leader, validator, sync = await self._lvs_extended(
			name='context',
			line=2,
			address=ADDR_CALLER_V02,
			calldata=base._calldata('context', ADDR_OBSERVER_V03),
			resolve_hook=resolve,
		)
		expected = {
			'contract': ADDR_OBSERVER_V03.as_hex,
			'sender': base.SENDER.as_hex,
			'origin': base.SENDER.as_hex,
			'signer': base.SENDER.as_hex,
			'stack': [ADDR_CALLER_V02.as_hex],
			'value': 0,
			'is_init': False,
		}
		for result in (leader, validator, sync):
			assert result.result_kind == host_fns.ResultCode.RETURN, result
			assert result.result_data == expected, (result.result_data, expected)
		assert routes == [ADDR_OBSERVER_V03] * 3, routes
		self.notes.append(('nested context', expected))

	async def _assert_permissions(self):
		leader, validator, sync = await self._lvs_extended(
			name='permissions',
			line=2,
			address=ADDR_CALLER_V02,
			calldata=base._calldata('permissions', ADDR_OBSERVER_V03, ADDR_HELPER_V03),
			resolve_hook=self._route_v03,
			permissions='wscnu',
		)
		for result in (leader, validator, sync):
			assert result.result_kind == host_fns.ResultCode.RETURN, result
			observed = result.result_data
			for denied in ('write', 'send', 'nondet'):
				assert observed[denied]['kind'] == 'SystemError', observed
				assert observed[denied]['value'] == '6: forbidden', observed
			assert observed['registered'].startswith('custom:'), observed
			assert observed['called'] == 11, observed
		self.notes.append(('nested permission observations', leader.result_data))

	async def _assert_debug_alias(self):
		top_level = await self._execute(
			name='debug-alias-top-level',
			line=3,
			address=ADDR_OBSERVER_V03,
			calldata=base._calldata('debug_alias'),
			code=None,
			is_init=False,
			resolve_hook=self._route_v03,
			apply_changes=False,
		)
		# Unsafe mode resolves the alias and gets as far as running it, so the
		# deliberately mismatched sandbox payload surfaces as a user error the
		# contract can see. Under disabled debug the same runner fails before
		# startup and is a vm error instead; the kind, not the code, is what
		# separates the two.
		assert top_level.result_kind == host_fns.ResultCode.USER_ERROR, top_level
		assert top_level.result_data == 'vm error: ' + str(
			public_abi.VmError.invalid_contract().runner().malformed()
		), top_level

		leader, validator, sync = await self._lvs_extended(
			name='debug-alias-nested',
			line=2,
			address=ADDR_CALLER_V02,
			calldata=base._calldata('debug_alias', ADDR_OBSERVER_V03),
			resolve_hook=self._route_v03,
		)
		for result in (leader, validator, sync):
			assert result.result_kind == host_fns.ResultCode.VM_ERROR, result
			assert result.result_data == str(
				public_abi.VmError.invalid_contract().runner().malformed()
			), result
		self.notes.append(
			(
				'nested debug observation',
				':test resolves at unsafe top level and is malformed_runner when nested',
			)
		)

	async def _assert_read_only_storage(self, permissions: str):
		reads: list[tuple[Address, public_abi.StorageType]] = []
		leader, validator, sync = await self._lvs_extended(
			name=f'storage-{permissions}',
			line=2,
			address=ADDR_CALLER_V02,
			calldata=base._calldata('read', ADDR_OBSERVER_V03),
			resolve_hook=self._route_v03,
			permissions=permissions,
			read_log=reads,
		)
		for result in (leader, validator, sync):
			assert result.result_kind == host_fns.ResultCode.RETURN, result
			assert result.result_data == 777, result
		assert any(account == ADDR_OBSERVER_V03 for account, _mode in reads), reads
		self.notes.append(('read-only nested storage', permissions))

	async def _assert_self_recursion(self, budget: int):
		expected_error = str(public_abi.VmError.out_of().vm_recursion())
		leader, validator, sync = await self._lvs_extended(
			name=f'self-recursion-{budget}',
			line=3,
			address=ADDR_SELF_V03,
			calldata=base._calldata('observe_recursion', 4),
			resolve_hook=self._route_v03,
			debug_initial_recursion=budget,
		)
		for result in (leader, validator, sync):
			if budget < 5:
				assert result.result_kind == host_fns.ResultCode.VM_ERROR, result
				assert result.result_data == expected_error, (
					budget,
					result.result_data,
					expected_error,
				)
			else:
				assert result.result_kind == host_fns.ResultCode.RETURN, result
				assert result.result_data == 'ok:5', (budget, result)
		self.notes.append(
			(
				'self recursion outcome',
				(
					budget,
					str(leader.result_kind),
					str(leader.result_data),
					leader.execution_hash.hex(),
				),
			)
		)

	async def _assert_deep_nesting(self, depth: int):
		# A chain deeper than the manager's cross-major bound is refused rather
		# than served, and every node refuses it at the same place.
		leader, validator, sync = await self._lvs_extended(
			name=f'deep-{depth}',
			line=3,
			address=ADDR_CHAIN_V03,
			calldata=base._calldata('hop', depth, ADDR_CHAIN_V02),
			resolve_hook=self._route_chain,
		)
		if depth > CROSS_MAJOR_RECURSION:
			for result in (leader, validator, sync):
				assert result.result_kind == host_fns.ResultCode.VM_ERROR, result
				assert result.result_data == 'out_of vm_recursion', (depth, result)
			outcome: int | str = 'out_of vm_recursion'
		else:
			expected = sum(3 if index % 2 == 0 else 2 for index in range(depth + 1))
			for result in (leader, validator, sync):
				assert result.result_kind == host_fns.ResultCode.RETURN, result
				assert result.result_data == expected, (depth, result, expected)
			outcome = expected
		self.notes.append(
			('deep alternating hash', (depth, outcome, leader.execution_hash.hex()))
		)

	async def _assert_error(self, label: str, target: Address):
		leader, validator, sync = await self._lvs_extended(
			name=f'error-{label}',
			line=2,
			address=ADDR_CALLER_V02,
			calldata=base._calldata('answer', target),
			resolve_hook=lambda address, state, major: (
				self._route(3) if address == target else self._route_v03(address, state, major)
			),
		)
		for result in (leader, validator, sync):
			assert result.result_kind in {
				host_fns.ResultCode.VM_ERROR,
				host_fns.ResultCode.USER_ERROR,
			}, (label, result)
		self.notes.append(
			(
				'nested error outcome',
				(label, str(leader.result_kind), str(leader.result_data)[:120]),
			)
		)

	async def _assert_undeployed_error(self):
		await self._assert_error('undeployed', ABSENT)

	async def _assert_non_contract_error(self):
		await self._assert_error('non-contract', base.SENDER)

	async def _assert_trap_error(self):
		await self._assert_error('trap', ADDR_TRAP_V03)

	async def _assert_user_error(self):
		await self._assert_error('user-error', ADDR_USER_ERROR_V03)

	async def _assert_fuel(self, fuel: int):
		leader, validator, sync = await self._lvs_extended(
			name=f'fuel-{fuel}',
			line=2,
			address=ADDR_CALLER_V02,
			calldata=base._calldata('read', ADDR_OBSERVER_V03),
			resolve_hook=self._route_v03,
			host_fuel=fuel,
		)
		for result in (leader, validator, sync):
			assert result.result_kind == host_fns.ResultCode.RETURN, result
			assert result.result_data == 777, result
		self.notes.append(
			('nested deterministic fuel hash', (fuel, leader.execution_hash.hex()))
		)


def collect(
	ctx: genvm_tool.tests.stage.collection.Context,
	*,
	manager_service: genvm_tool.tests.stage.collection.Service,
) -> None:
	cases = [
		('validator-mode', '_assert_validator_mode', ('chain-v03', 'chain-v02'), ()),
		('message-context', '_assert_context', ('caller-v02', 'observer-v03'), ()),
		(
			'permissions',
			'_assert_permissions',
			('caller-v02', 'observer-v03', 'helper-v03'),
			(),
		),
		('debug-alias', '_assert_debug_alias', ('caller-v02', 'observer-v03'), ()),
		*[
			(
				f'read-only-storage-{permissions}',
				'_assert_read_only_storage',
				('caller-v02', 'observer-v03'),
				(permissions,),
			)
			for permissions in ('c', 'wscn')
		],
		*[
			(
				f'self-recursion-{budget}',
				'_assert_self_recursion',
				('self-v03',),
				(budget,),
			)
			for budget in (0, 1, 2, 4, 5, 6)
		],
		*[
			(
				f'deep-nesting-{depth}',
				'_assert_deep_nesting',
				('chain-v03', 'chain-v02'),
				(depth,),
			)
			for depth in (1, 4, 8)
		],
		('error-undeployed', '_assert_undeployed_error', ('caller-v02',), ()),
		('error-non-contract', '_assert_non_contract_error', ('caller-v02',), ()),
		('error-trap', '_assert_trap_error', ('caller-v02', 'trap-v03'), ()),
		(
			'error-user',
			'_assert_user_error',
			('caller-v02', 'user-error-v03'),
			(),
		),
		*[
			(
				f'fuel-{fuel}',
				'_assert_fuel',
				('caller-v02', 'observer-v03'),
				(fuel,),
			)
			for fuel in (0, 1, 1000, 2**32)
		],
	]
	for slug, method, fixtures, arguments in cases:
		desc = genvm_tool.tests.test.Description(
			name=f'tests/system/cross-major-observability/{slug}',
			needed_services=frozenset({manager_service}),
			tags=frozenset(
				{
					'integration',
					'stable',
					'feature-version-routing-cross-major',
					'feature-observability',
				}
			),
		)
		ctx.add_case(
			ObservabilityCase(
				description=desc,
				shared=ctx.shared,
				manager_service=manager_service,
				method=method,
				fixtures=fixtures,
				arguments=arguments,
			)
		)
