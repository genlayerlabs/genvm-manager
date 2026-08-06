"""Cross-major CallContract observability and determinism integration tests.

This case reuses the real-manager harness from ``tests/system/cross-major``
without adding more concerns to that already-large case.
"""

import importlib.util
import json
import pickle
import sys
import typing
from dataclasses import dataclass
from pathlib import Path

import genvm_tool.tests
import genvm_tool.tests.stage.collection
from gvm_extra.mock_host import MockStorage
from origin import public_abi
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


CALLER_V02 = base._contract_source(
	2,
	"""
	from genlayer import *


	class Contract(gl.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def context(self, target: Address):
			return gl.get_contract_at(target).view().context()

		@gl.public.view
		def permissions(self, target: Address, helper: Address):
			return gl.get_contract_at(target).view().permissions(helper)

		@gl.public.view
		def debug_alias(self, target: Address):
			return gl.get_contract_at(target).view().debug_alias()

		@gl.public.view
		def read(self, target: Address) -> int:
			return gl.get_contract_at(target).view().read()

		@gl.public.view
		def answer(self, target: Address) -> int:
			return gl.get_contract_at(target).view().answer()
	""",
)

OBSERVER_V03 = base._contract_source(
	3,
	f"""
	import genlayer as gl
	import _genlayer_wasi as wasi
	from genlayer.types import Address, u32


	class Contract(gl.contract.Contract):
		value: u32

		def __init__(self):
			self.value = 777

		@gl.public.view
		def context(self):
			return {{
				'contract': gl.message.contract_address.as_hex,
				'sender': gl.message.sender_address.as_hex,
				'origin': gl.message.origin_address.as_hex,
				'signer': gl.message.signer_address.as_hex,
				'stack': [address.as_hex for address in gl.message.stack],
				'value': int(gl.message.value),
				'is_init': gl.message.is_init,
			}}

		@gl.public.view
		def read(self) -> int:
			return self.value

		@gl.public.view
		def permissions(self, helper: Address):
			def attempt(action):
				try:
					value = action()
					return {{'kind': 'allowed', 'value': str(value)}}
				except OSError as exc:
					return {{'kind': 'oserror', 'errno': exc.errno}}
				except Exception as exc:
					return {{'kind': type(exc).__name__, 'value': str(exc)}}

			write = attempt(
				lambda: wasi.storage_write(b'\\x77' * 32, 0, b'forbidden')
			)
			send = attempt(
				lambda: gl.contract.get_at(Address(b'\\x66' * 20)).emit().ping()
			)
			nondet = attempt(
				lambda: gl.vm.run_nondet(lambda: 1, lambda _result: True)
			)

			registered = gl.vm.register_runner(
				b'# {{ "Depends": "py-genlayer:{base.PY_GENLAYER_V03}" }}\\n'
				b'print("registered")\\n'
			)
			called = gl.contract.get_at(helper).view().answer()
			return {{
				'write': write,
				'send': send,
				'nondet': nondet,
				'registered': registered,
				'called': called,
			}}

		@gl.public.view
		def debug_alias(self) -> int:
			result = gl.vm.spawn_sandbox(lambda: 1, runner='py-genlayer:test')
			return gl.vm.unpack_result(result)
	""",
)

HELPER_V03 = base._contract_source(
	3,
	"""
	import genlayer as gl


	class Contract(gl.contract.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def answer(self) -> int:
			return 11
	""",
)

SELF_V03 = base._contract_source(
	3,
	"""
	import genlayer as gl


	class Contract(gl.contract.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def recurse(self, depth: int) -> int:
			if depth == 0:
				return 1
			return 1 + gl.contract.get_at(self.address).view().recurse(depth - 1)

		@gl.public.view
		def observe_recursion(self, depth: int) -> str:
			try:
				return 'ok:' + str(self.recurse(depth))
			except gl.vm.UserError as exc:
				return 'caught:' + str(exc.data)
	""",
)

TRAP_V03 = base._contract_source(
	3,
	"""
	import genlayer as gl


	class Contract(gl.contract.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def answer(self) -> int:
			raise RuntimeError('callee trap')
	""",
)

USER_ERROR_V03 = base._contract_source(
	3,
	"""
	import genlayer as gl


	class Contract(gl.contract.Contract):
		def __init__(self):
			pass

		@gl.public.view
		def answer(self) -> int:
			gl.vm.UserError.immediate('nested user error')
	""",
)

CHAIN_V03 = base._contract_source(
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
			return 3 + gl.contract.get_at(other).view().hop(
				depth - 1, gl.message.contract_address
			)
	""",
)

CHAIN_V02 = base._contract_source(
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
			return 2 + gl.get_contract_at(other).view().hop(
				depth - 1, gl.message.contract_address
			)
	""",
)


@dataclass
class ObservabilityCase(genvm_tool.tests.test.Case):
	description: genvm_tool.tests.test.Description
	shared: genvm_tool.tests.SharedContext

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
		work_dir = self.case.shared.artifacts_dir / 'cross-major-observability'
		self.storage_path = work_dir / 'storage.pickle'
		work_dir.mkdir(parents=True, exist_ok=True)
		with open(self.storage_path, 'wb') as storage_file:
			pickle.dump(MockStorage(), storage_file)

		async with base.ManagerProc(
			build_dir=self.build_dir,
			work_dir=work_dir / 'manager',
		) as manager:
			self.manager = manager

			self.phase = 'deploy observability fixtures'
			for line, address, code in (
				(2, ADDR_CALLER_V02, CALLER_V02),
				(3, ADDR_OBSERVER_V03, OBSERVER_V03),
				(3, ADDR_HELPER_V03, HELPER_V03),
				(3, ADDR_SELF_V03, SELF_V03),
				(3, ADDR_TRAP_V03, TRAP_V03),
				(3, ADDR_USER_ERROR_V03, USER_ERROR_V03),
				(3, ADDR_CHAIN_V03, CHAIN_V03),
				(2, ADDR_CHAIN_V02, CHAIN_V02),
			):
				await self._deploy(line, address, code)

			self.phase = 'leader and validator modes are genuine'
			await self._assert_validator_mode()

			self.phase = 'callee observes the preserved nested message'
			await self._assert_context()

			self.phase = 'nested permissions and disabled debug are observable'
			await self._assert_permissions_and_debug()

			self.phase = 'read-only roots and nested storage reads agree'
			await self._assert_read_only_storage()

			self.phase = 'self-call recursion budgets agree in lvs'
			await self._assert_self_recursion()

			self.phase = 'deep alternating nesting agrees in lvs'
			await self._assert_deep_nesting()

			self.phase = 'contract-controlled nested errors are not internal'
			await self._assert_error_propagation()

			self.phase = 'imported deterministic fuel agrees in lvs'
			await self._assert_fuel()

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
						code=kwargs.get('code'),
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
					and result.result_kind == public_abi.ResultCode.RETURN
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
			assert result.result_kind != public_abi.ResultCode.INTERNAL_ERROR, (
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
		assert bogus.result_kind == public_abi.ResultCode.VM_ERROR, bogus

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
			assert result.result_kind == public_abi.ResultCode.RETURN, result
			assert result.result_data == expected, (result.result_data, expected)
		assert routes == [ADDR_OBSERVER_V03] * 3, routes
		self.notes.append(('nested context', expected))

	async def _assert_permissions_and_debug(self):
		leader, validator, sync = await self._lvs_extended(
			name='permissions',
			line=2,
			address=ADDR_CALLER_V02,
			calldata=base._calldata('permissions', ADDR_OBSERVER_V03, ADDR_HELPER_V03),
			resolve_hook=self._route_v03,
			permissions='wscnu',
		)
		for result in (leader, validator, sync):
			assert result.result_kind == public_abi.ResultCode.RETURN, result
			observed = result.result_data
			for denied in ('write', 'send', 'nondet'):
				assert observed[denied]['kind'] == 'SystemError', observed
				assert observed[denied]['value'] == '6: forbidden', observed
			assert observed['registered'].startswith('custom:'), observed
			assert observed['called'] == 11, observed
		self.notes.append(('nested permission observations', leader.result_data))

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
		# Unsafe mode resolves the alias and gets as far as running it. The
		# deliberately mismatched sandbox payload is then an ordinary user-visible
		# invalid-contract result, rather than the absent/malformed runner emitted
		# before startup under disabled debug.
		assert top_level.result_kind == public_abi.ResultCode.USER_ERROR, top_level
		assert top_level.result_data == 'vm error: invalid_contract', top_level

		leader, validator, sync = await self._lvs_extended(
			name='debug-alias-nested',
			line=2,
			address=ADDR_CALLER_V02,
			calldata=base._calldata('debug_alias', ADDR_OBSERVER_V03),
			resolve_hook=self._route_v03,
		)
		for result in (leader, validator, sync):
			assert result.result_kind == public_abi.ResultCode.VM_ERROR, result
			assert result.result_data == str(
				public_abi.VmError.invalid_contract().malformed_runner()
			), result
		self.notes.append(
			(
				'nested debug observation',
				':test resolves at unsafe top level and is malformed_runner when nested',
			)
		)

	async def _assert_read_only_storage(self):
		for permissions in ('c', 'wscn'):
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
				assert result.result_kind == public_abi.ResultCode.RETURN, result
				assert result.result_data == 777, result
			assert any(account == ADDR_OBSERVER_V03 for account, _mode in reads), reads
		self.notes.append(('read-only nested storage', 'c and wscn both returned 777'))

	async def _assert_self_recursion(self):
		expected_error = str(public_abi.VmError.out_of().vm_recursion())
		outcomes = []
		for budget in (0, 1, 2, 4, 5, 6):
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
					assert result.result_kind == public_abi.ResultCode.VM_ERROR, result
					assert result.result_data == expected_error, (
						budget,
						result.result_data,
						expected_error,
					)
				else:
					assert result.result_kind == public_abi.ResultCode.RETURN, result
					assert result.result_data == 'ok:5', (budget, result)
			outcomes.append(
				(
					budget,
					str(leader.result_kind),
					str(leader.result_data),
					leader.execution_hash.hex(),
				)
			)
		self.notes.append(('self recursion outcomes', outcomes))

	async def _assert_deep_nesting(self):
		outcomes = []
		for depth in (1, 4, 8):
			leader, validator, sync = await self._lvs_extended(
				name=f'deep-{depth}',
				line=3,
				address=ADDR_CHAIN_V03,
				calldata=base._calldata('hop', depth, ADDR_CHAIN_V02),
				resolve_hook=self._route_chain,
			)
			expected = sum(3 if index % 2 == 0 else 2 for index in range(depth + 1))
			for result in (leader, validator, sync):
				assert result.result_kind == public_abi.ResultCode.RETURN, result
				assert result.result_data == expected, (depth, result, expected)
			outcomes.append((depth, expected, leader.execution_hash.hex()))
		self.notes.append(('deep alternating hashes', outcomes))

	async def _assert_error_propagation(self):
		cases = (
			('undeployed', ABSENT),
			('non-contract', base.SENDER),
			('trap', ADDR_TRAP_V03),
			('user-error', ADDR_USER_ERROR_V03),
		)
		observed = []
		for label, target in cases:
			leader, validator, sync = await self._lvs_extended(
				name=f'error-{label}',
				line=2,
				address=ADDR_CALLER_V02,
				calldata=base._calldata('answer', target),
				resolve_hook=lambda address, state, major, target=target: (
					self._route(3)
					if address == target
					else self._route_v03(address, state, major)
				),
			)
			for result in (leader, validator, sync):
				assert result.result_kind in {
					public_abi.ResultCode.VM_ERROR,
					public_abi.ResultCode.USER_ERROR,
				}, (label, result)
			observed.append((label, str(leader.result_kind), str(leader.result_data)[:120]))
		self.notes.append(('nested error outcomes', observed))

	async def _assert_fuel(self):
		outcomes = []
		for fuel in (0, 1, 1000, 2**32):
			leader, validator, sync = await self._lvs_extended(
				name=f'fuel-{fuel}',
				line=2,
				address=ADDR_CALLER_V02,
				calldata=base._calldata('read', ADDR_OBSERVER_V03),
				resolve_hook=self._route_v03,
				host_fuel=fuel,
			)
			for result in (leader, validator, sync):
				assert result.result_kind == public_abi.ResultCode.RETURN, result
				assert result.result_data == 777, result
			outcomes.append((fuel, leader.execution_hash.hex()))
		self.notes.append(('nested deterministic fuel hashes', outcomes))


def collect(ctx: genvm_tool.tests.stage.collection.Context) -> None:
	desc = genvm_tool.tests.test.Description(
		name='tests/system/cross-major-observability',
		tags=frozenset({'integration', 'stable', 'cross-major'}),
		console_pool=True,
	)
	ctx.add_case(ObservabilityCase(description=desc, shared=ctx.shared))
