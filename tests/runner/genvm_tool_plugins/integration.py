"""
Integration test plugin for genvm-tool test.

This plugin collects and runs integration tests from .jsonnet files in tests/integration/.
It uses the same MockHost/base_host infrastructure as the old runner.
"""

import base64
import contextlib
import difflib
import gzip
import hashlib
import io
import itertools
import json
import os
import pickle
import re
import shutil
import sqlite3
import sys
import threading
import time
import typing
from dataclasses import dataclass
from pathlib import Path

import genvm_tool.gvm32
import genvm_tool.tests
import origin.base_host as base_host
import origin.calldata as gvm_calldata
import origin.fees as fees
import origin.log_asserts as log_asserts
import origin.logger as origin_logger
import origin.public_abi as public_abi
from genvm_tool.tests.exec.command import Command, RunMode
from gvm_extra.mock_host import MockHost as MockHost
from gvm_extra.mock_host import MockStorage as MockStorage
from origin.calldata import Address

if typing.TYPE_CHECKING:
	import _jsonnet

# Get the local context
local_ctx = genvm_tool.tests.stage.configuration.current_context()

# Load build info
build_info = json.loads(
	local_ctx.shared.root_dir.joinpath('build', 'info.json').read_text()
)

BUILD_DIR = Path(build_info['build_dir'])

local_ctx.run_parser.add_argument(
	'--ignore-hash',
	action='store_true',
	default=False,
	help='Skip the committed .hash golden sidecars (no comparison, no creation). Leader-vs-validator/sync comparison still runs.',
)


def _is_ignore_hash_enabled() -> bool:
	return '--ignore-hash' in sys.argv


# Integration cases live per-version in each executor checkout
# (executors/<line>.x/tests/integration). `integration_test` iterates the active
# version lines from .genvm-monorepo-root and runs each line's suite against that
# line's own executor (see `integration_test_single_executor`, which resolves the
# line's cases dir and reroute target). The harness itself (templates, runner) is
# version-independent and lives in the manager root.
TEMPLATES_DIR = local_ctx.shared.root_dir.joinpath('tests', 'templates')

# Default environment for tests
default_env = {
	k: v
	for k, v in os.environ.items()
	if genvm_tool.tests.util.environ.DEFAULT_FILTER(k, v)
}

# `prepare` scripts run `cargo build`; the host `rustc` used for build
# scripts/proc-macros needs libz etc. on LD_LIBRARY_PATH. Fold in
# CARGO_LD_LIBRARY_PATH the same way cargo.py does for its own commands.
_cargo_ld_library_path = os.environ.get('CARGO_LD_LIBRARY_PATH', None)
_base_ld_library_path = default_env.get('LD_LIBRARY_PATH', None)
_new_ld_library_path = ':'.join(
	filter(None, [_cargo_ld_library_path, _base_ld_library_path])
)
if _new_ld_library_path:
	default_env['LD_LIBRARY_PATH'] = _new_ld_library_path

# After the split, build artifacts live in the manager (umbrella) root, while
# rust-contract `prepare` scripts live in the executor checkout (a submodule)
# and can no longer reach build/info.json via a relative parents[] walk. Hand
# them the absolute location explicitly.
default_env['GENVM_BUILD_INFO'] = str(
	local_ctx.shared.root_dir.joinpath('build', 'info.json')
)


class _SavedLog(typing.TypedDict):
	level: str
	msg: str
	kwargs: dict


_SHORT_TEXT_LIMIT = 2000
_SHORT_LIST_LIMIT = 12
_NOISY_CONTEXT_KEYS = frozenset(
	{
		'exp',
		'got',
		'genvm_log',
		'log',
		'logs',
		'semantics',
		'stderr',
		'stdout',
	}
)


def _shorten_text(value: str, limit: int = _SHORT_TEXT_LIMIT) -> str:
	if len(value) <= limit:
		return value
	omitted = len(value) - limit
	return f'<truncated {omitted} chars>\n{value[-limit:]}'


def _shorten_value(value: typing.Any) -> typing.Any:
	if isinstance(value, str):
		return _shorten_text(value)
	if isinstance(value, bytes):
		if len(value) <= _SHORT_TEXT_LIMIT:
			return value
		return value[:_SHORT_TEXT_LIMIT] + b'<truncated>'
	if isinstance(value, BaseException):
		return {
			'type': value.__class__.__name__,
			'message': str(value),
		}
	if isinstance(value, list):
		if len(value) <= _SHORT_LIST_LIMIT:
			return value
		return [
			*value[:_SHORT_LIST_LIMIT],
			f'<truncated {len(value) - _SHORT_LIST_LIMIT} items>',
		]
	return value


def _short_failure_context(
	context: dict[str, typing.Any], artifacts_dir: Path | None = None, *, ci: bool
) -> dict[str, typing.Any]:
	if ci:
		return context

	short = {
		k: _shorten_value(v) for k, v in context.items() if k not in _NOISY_CONTEXT_KEYS
	}
	for key in ('stderr', 'stdout'):
		if key in context and context[key]:
			short[f'{key}_tail'] = _shorten_value(context[key])
	if 'logs' in context:
		short['log_count'] = len(context['logs'])
	if 'genvm_log' in context:
		short['genvm_log_count'] = len(context['genvm_log'])
	if artifacts_dir is not None:
		short['artifacts_dir'] = str(artifacts_dir)
	short['hint'] = 'run with --ci for full inline failure context'
	return short


def _emit_ci_error_annotation(jsonnet_path: Path, reason: str) -> None:
	"""Write a GitHub Actions error annotation directly to stdout."""
	# GitHub Actions command parsing uses percent-encoded newlines and carriage
	# returns inside the annotation body.
	reason = reason.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
	sys.stdout.write(f'::error file={jsonnet_path}::Test failed {reason}\n')
	sys.stdout.flush()


def _make_log_adapter(formatter: genvm_tool.tests.Formatter) -> 'origin_logger.Logger':
	class _FormatterLoggerAdapter(origin_logger.Logger):
		"""Adapts genvm_tool.formatter.Formatter to base_host.Logger interface."""

		saved_logs: list[_SavedLog]

		def __init__(self, formatter: genvm_tool.tests.Formatter):
			self._formatter = formatter

			self.saved_logs = []

		def log(self, level: str, msg: str, **kwargs) -> None:
			fmt_level = genvm_tool.tests.Formatter.Level.from_str(level)
			self._formatter.log(fmt_level, msg, **kwargs)
			self.saved_logs.append(
				{
					'level': level,
					'msg': msg,
					'kwargs': kwargs,
				}
			)

	global make_adapter
	make_adapter = _FormatterLoggerAdapter

	return make_adapter(formatter)


def _unfold_conf(x: typing.Any, vars: dict[str, str]) -> typing.Any:
	"""Recursively substitute variables in configuration."""
	if isinstance(x, str):
		return re.sub(r'\$\{[a-zA-Z\-_]+\}', lambda m: vars[m.group()[2:-1]], x)
	if isinstance(x, list):
		return [_unfold_conf(item, vars) for item in x]
	if isinstance(x, dict):
		return {k: _unfold_conf(v, vars) for k, v in x.items()}
	return x


def _flatten_tree(
	entries: list[dict],
) -> list[tuple[str, str | None, dict]]:
	"""
	DFS-traverse the step tree and return (tree_path, depends_on_tree_path, step_conf) tuples.

	The depends_on field is computed in the jsonnet template (util.jsonnet expandModes).
	A null depends_on means the step depends on /prepare.
	"""
	result: list[tuple[str, str | None, dict]] = []
	for entry in entries:
		step_conf = {k: v for k, v in entry.items() if k != 'next'}
		tree_path = step_conf['tree_path']
		depends_on = step_conf.get('depends_on')

		result.append((tree_path, depends_on, step_conf))
		if 'next' in entry:
			result.extend(_flatten_tree(entry['next']))
	return result


_TOP_LEVEL_METADATA_KEYS = frozenset({'entry', 'tags', 'debug_mode'})

# Ordered as in `genvm_common::DebugMode`; each level adds to the previous one.
# `unsafe` is what every case has always run with: contracts name their runner
# as `py-genlayer:test`, and that alias only resolves from `unsafe` up.
_DEBUG_MODES = ('disabled', 'safe', 'safe-unbounded', 'unsafe', 'unsafe-tracing')
_DEBUG_MODE_DEFAULT = 'unsafe'


@dataclass
class IntegrationPrepareCase(genvm_tool.tests.test.Case):
	"""Test case for the preparation/setup phase of an integration test."""

	description: genvm_tool.tests.test.Description
	jsonnet_path: Path
	top_level_conf: dict
	tmp_dir: Path
	ci: bool

	hidden: bool = True

	async def into_steps(self) -> list[genvm_tool.tests.exec.step.Step]:
		return [IntegrationSetupStep(self)]


@dataclass
class IntegrationSingleCase(genvm_tool.tests.test.Case):
	"""Test case for a single step of an integration test."""

	description: genvm_tool.tests.test.Description
	jsonnet_path: Path
	cases_dir: Path
	reroute_to: str
	manager_service: genvm_tool.tests.stage.collection.Service
	tree_path: str
	parent_tree_path: str | None
	single_conf: dict
	total_steps: int
	tmp_dir: Path
	max_attempts: int
	debug_mode: str = _DEBUG_MODE_DEFAULT
	save_hashes: bool = True
	is_benchmark: bool = False
	ci: bool = False

	async def into_steps(self) -> list[genvm_tool.tests.exec.step.Step]:
		step = IntegrationSingleStep(self)
		if self.is_benchmark:
			steps: list[genvm_tool.tests.exec.step.Step] = []
			for i in range(10):
				steps.append(CheckInterruptedStep())
				steps.append(genvm_tool.tests.test.BenchMeasureStep())
				steps.append(step)
			steps.append(
				genvm_tool.tests.test.BenchCollectStep(
					local_ctx.shared.printer, test_name=self.description.name
				)
			)
			return steps
		return [step]


class CheckInterruptedStep(genvm_tool.tests.exec.step.Python):
	def to_str(self):
		return '<check interrupted>'

	async def run(self, previous_results: list[typing.Any]):
		if local_ctx.shared.is_interrupted:
			raise genvm_tool.tests.test.FinishedEarlyException(
				genvm_tool.tests.test.Result(
					passed=False,
					context={'reason': 'interrupted'},
					elapsed_seconds=0,
				)
			)


class IntegrationSkipStep(genvm_tool.tests.exec.step.Python):
	"""Returns a skipped result for tests with .skip file."""

	def __init__(self, test_name: str):
		self._test_name = test_name

	def to_str(self) -> str:
		return f'<skip: {self._test_name}>'

	async def run(
		self, previous_results: list[typing.Any]
	) -> genvm_tool.tests.test.Result:
		local_ctx.shared.logger.warning(
			'Test skipped',
			test_name=self._test_name,
		)
		return genvm_tool.tests.test.Result(
			passed=True,
			context={'skipped': True},
			elapsed_seconds=0,
		)


class IntegrationSetupStep(genvm_tool.tests.exec.step.Python):
	"""Sets up the test environment: temp dir, prepare script, base storage."""

	def __init__(self, case: IntegrationPrepareCase):
		self._case = case

	def to_str(self) -> str:
		return f'<setup: {self._case.jsonnet_path.name}>'

	async def run(
		self, previous_results: list[typing.Any]
	) -> genvm_tool.tests.test.Result:
		tmp_dir = self._case.tmp_dir
		top_level_conf = self._case.top_level_conf

		# Set up temp directory. tmp_dir is this test's per-test log root, so we
		# clear stale step artifacts but keep this prepare step's own (open) log
		# directory; per-step dirs are (re)created when each step runs.
		keep = local_ctx.shared.case_dir_for(self._case.description.name)
		if tmp_dir.exists():
			for child in tmp_dir.iterdir():
				if child == keep:
					continue
				if child.is_dir():
					shutil.rmtree(child, ignore_errors=True)
				else:
					child.unlink(missing_ok=True)
		tmp_dir.mkdir(exist_ok=True, parents=True)

		# Run preparation if needed
		if 'prepare' in top_level_conf:
			cmd = Command(
				args=[sys.executable, top_level_conf['prepare']],
				cwd=self._case.jsonnet_path.parent,
				env=default_env,
			)
			result = await cmd.run(local_ctx.shared, mode=RunMode.SILENT)
			if result.exit_code != 0:
				context = _short_failure_context(
					{
						'reason': 'prepare script failed',
						'exit_code': result.exit_code,
						'stdout': result.stdout,
						'stderr': result.stderr,
						'log': result.stderr,
					},
					tmp_dir,
					ci=self._case.ci,
				)
				if self._case.ci:
					_emit_ci_error_annotation(
						self._case.jsonnet_path,
						str(context.get('reason', 'prepare script failed')),
					)
				raise genvm_tool.tests.test.FinishedEarlyException(
					result=genvm_tool.tests.test.Result(
						passed=False,
						context=context,
						elapsed_seconds=0,
					)
				)

		# Set up base storage
		base_mock_storage = MockStorage()
		if storage_json := top_level_conf.get('storage_json'):
			storage_b64 = json.loads(Path(storage_json).read_text())
			base_mock_storage._storages = {
				Address(a): {
					base64.b64decode(k): bytearray(base64.b64decode(v)) for k, v in kv.items()
				}
				for a, kv in storage_b64.items()
			}

		empty_storage = tmp_dir.joinpath('empty-storage.pickle')
		with open(empty_storage, 'wb') as f:
			pickle.dump(base_mock_storage, f)

		return genvm_tool.tests.test.Result(
			passed=True,
			context={},
			elapsed_seconds=0,
		)


FAKE_TX_ID = '0x' + '00' * 32
FAKE_NODE_ADDRESS = '0xE840F4456F4cD28C4f54d0F8AfA2C0DBf43e4d29'
FAKE_NODE_PRIVATE_KEY = (
	'81bd0b16ba7f9a06ca3e0e54796018b4792dbc406a93421bb8789af2dd139809'
)
FAKE_NODE_PUBLIC_KEY = '6478c39d71a8e469a2dfc5f467ab48e449012308228ab81aa2341107ea7bb3324ab8d4169d49f4705a35b7271475f6d81e210aa2ff35fea4d74d83d25ec6599c'
SIGNER_URL = 'https://test-server.genlayer.com/genvm/sign'


def _get_diffs[T](exp: T, got: T, dump: typing.Callable[[T], str]) -> dict | None:
	if exp == got:
		return None
	exp_txt = dump(exp)
	got_txt = dump(got)
	diff = difflib.unified_diff(
		exp_txt.splitlines(keepends=False),
		got_txt.splitlines(keepends=False),
	)

	return {
		'exp': exp,
		'got': got,
		'diff': '\n'.join(itertools.islice(diff, 5)),
	}


def _calldata_to_fancy_str(calldata: bytes) -> str:
	cd = gvm_calldata.decode(calldata)
	buf = io.StringIO()
	genvm_tool.formatter.TextFormatter(genvm_tool.formatter.NoLockTextIO(buf)).dump(
		genvm_tool.formatter.Formatter.Level.ERROR, 'calldata', calldata=cd
	)
	return buf.getvalue()


class Context(base_host.Context):
	logger: base_host.Logger

	def __init__(self, logger: base_host.Logger):
		self.logger = logger

	def on_genvm_success(self): ...
	def on_genvm_failure(self): ...

	def add_stat(self, key: str, value: typing.Any):
		self.logger.debug('stat', key=key, val=value)

	def get_timeout(
		self, action: base_host.TimeoutAction, type: base_host.TimeoutType
	) -> float | None:
		if type == base_host.TimeoutType.CONNECT_S:
			return 5.0
		return None


class IntegrationSingleStep(genvm_tool.tests.exec.step.Python):
	"""Executes a single step of an integration test."""

	def __init__(self, case: IntegrationSingleCase):
		self._test_case = case
		self._tree_path = case.tree_path
		self._parent_tree_path = case.parent_tree_path
		self._single_conf = case.single_conf
		self._total_steps = case.total_steps
		self._tmp_dir = case.tmp_dir
		self._max_attempts = case.max_attempts
		self._debug_mode = case.debug_mode
		self._save_hashes = case.save_hashes
		self._ci = case.ci

	def to_str(self) -> str:
		return f'<step {self._tree_path}: {self._test_case.jsonnet_path.name}>'

	async def run(
		self, previous_results: list[typing.Any]
	) -> genvm_tool.tests.test.Result:
		empty_storage = self._tmp_dir.joinpath('empty-storage.pickle')
		jsonnet_path = self._test_case.jsonnet_path

		for attempt in range(self._max_attempts):
			sub_logger = _make_log_adapter(local_ctx.shared.logger)
			result = await self._run_single_step(empty_storage, sub_logger)
			if result['passed']:
				return genvm_tool.tests.test.Result(
					passed=True,
					context=result.get('context', {}),
					elapsed_seconds=0,
				)

			if local_ctx.shared.is_interrupted or attempt + 1 >= self._max_attempts:
				# Raise FinishedEarlyException to stop subsequent steps
				context = result.get('context', {})
				context['logs'] = sub_logger.saved_logs
				context = _short_failure_context(
					context,
					self._tmp_dir.joinpath(self._tree_path),
					ci=self._ci,
				)
				if self._ci:
					_emit_ci_error_annotation(
						jsonnet_path,
						str(context.get('reason', 'unknown error')),
					)
				raise genvm_tool.tests.test.FinishedEarlyException(
					result=genvm_tool.tests.test.Result(
						passed=False,
						context=context,
						elapsed_seconds=0,
					)
				)

			local_ctx.shared.logger.warning(
				'Unstable test failed',
				attempt=attempt + 1,
				max_attempts=self._max_attempts,
				test_name=str(self._test_case.description.name),
				tree_path=self._tree_path,
				context=_short_failure_context(
					result.get('context', {}),
					self._tmp_dir.joinpath(self._tree_path),
					ci=self._ci,
				),
			)

		# Should not reach here
		raise genvm_tool.tests.test.FinishedEarlyException(
			result=genvm_tool.tests.test.Result(passed=False, context={}, elapsed_seconds=0)
		)

	async def _run_single_step(
		self, empty_storage: Path, logger: 'origin_logger.Logger'
	) -> dict:
		single_conf = pickle.loads(pickle.dumps(self._single_conf))  # Deep copy
		jsonnet_path = self._test_case.jsonnet_path
		tree_path = self._tree_path
		parent_tree_path = self._parent_tree_path

		my_tmp_dir = self._tmp_dir.joinpath(tree_path)
		suff = f'.{tree_path}'

		my_tmp_dir.mkdir(exist_ok=True, parents=True)

		# Set up storage paths
		if parent_tree_path is None:
			pre_storage = empty_storage
		else:
			pre_storage = self._tmp_dir.joinpath(parent_tree_path + '_storage.pickle')
		post_storage = my_tmp_dir.parent.joinpath(my_tmp_dir.name[:-1] + '_storage.pickle')

		config_str = json.dumps(single_conf, indent=2)
		my_tmp_dir.joinpath('config.json').write_text(config_str)

		# Prepare calldata
		calldata_eval_vars = {}
		calldata_eval_vars['Address'] = gvm_calldata.Address
		calldata_eval_vars['true'] = True
		calldata_eval_vars['false'] = False

		calldata_bytes = gvm_calldata.encode(
			eval(
				single_conf['calldata'],
				calldata_eval_vars,
				single_conf.get('vars', {}).copy(),
			)
		)

		# Process code file
		code_path = single_conf.get('code')
		code = None
		if code_path is not None:
			if code_path.endswith('.wat'):
				out_path = my_tmp_dir.joinpath(Path(code_path).with_suffix('.wasm').name)
				cmd = Command(
					args=[
						'wat2wasm',
						'--enable-tail-call',
						'--enable-annotations',
						'-o',
						str(out_path),
						code_path,
					],
					cwd=my_tmp_dir,
					env=default_env,
				)
				result = await cmd.run(local_ctx.shared, mode=RunMode.SILENT)
				if result.exit_code != 0:
					return {
						'passed': False,
						'context': {
							'reason': 'wat2wasm failed',
							'tree_path': tree_path,
							'exit_code': result.exit_code,
							'stdout': result.stdout,
							'stderr': result.stderr,
						},
					}
				code_path = str(out_path)
			code = Path(code_path).read_bytes()

		# Process message addresses
		single_conf['message']['contract_address'] = Address(
			single_conf['message']['contract_address']
		)
		single_conf['message']['sender_address'] = Address(
			single_conf['message']['sender_address']
		)
		single_conf['message']['origin_address'] = Address(
			single_conf['message']['origin_address']
		)
		single_conf['message']['signer_address'] = Address(
			single_conf['message']['signer_address']
		)

		path_to_which_leader_puts_result = my_tmp_dir.parent.joinpath(
			my_tmp_dir.name[:-1] + '_leader_nondet.pickle'
		)

		# Set up paths
		rel_path = jsonnet_path.relative_to(self._test_case.cases_dir)
		mock_sock_path = Path('/tmp', 'genvm-test', rel_path.with_suffix(f'.sock{suff}'))
		mock_sock_path.parent.mkdir(exist_ok=True, parents=True)

		is_leader = single_conf.get('mode') == 'l'

		# Load leader nondet results if available (for v/s modes)
		if is_leader:
			leader_nondet = None
		else:
			leader_nondet = single_conf.get('leader_nondet', None)
			if leader_nondet is None:
				with open(path_to_which_leader_puts_result, 'rb') as f:
					leader_nondet = pickle.load(f)
			if leader_nondet is not None:
				encoded_nondet = []
				for res in leader_nondet:
					if isinstance(res, (bytes, bytearray, memoryview)):
						encoded_nondet.append(bytes(res))
					elif res['kind'] == 'return':
						encoded_nondet.append(
							bytes([public_abi.ResultCode.RETURN]) + gvm_calldata.encode(res['value'])
						)
					elif res['kind'] == 'user_error':
						encoded_nondet.append(
							bytes([public_abi.ResultCode.USER_ERROR])
							+ gvm_calldata.encode(res['value'])
						)
					elif res['kind'] == 'vm_error':
						encoded_nondet.append(
							bytes([public_abi.ResultCode.VM_ERROR]) + res['value'].encode('utf-8')
						)
					elif res['kind'] == 'rollback':
						# v0.2.x: a rollback is a user error carrying a plain UTF-8
						# string (v0.2 user errors are string-only).
						encoded_nondet.append(
							bytes([public_abi.ResultCode.USER_ERROR]) + res['value'].encode('utf-8')
						)
					elif res['kind'] == 'contract_error':
						# v0.2.x: a vm error carrying a plain UTF-8 string.
						encoded_nondet.append(
							bytes([public_abi.ResultCode.VM_ERROR]) + res['value'].encode('utf-8')
						)
					elif res['kind'] == 'raw':
						encoded_nondet.append(bytes(res['value']))
					else:
						raise ValueError(f'unknown leader_nondet kind: {res["kind"]}')
				leader_nondet = encoded_nondet

		# Create mock host
		running_address = single_conf['message']['contract_address']
		host = MockHost(
			path=str(mock_sock_path),
			storage_path_post=post_storage,
			storage_path_pre=pre_storage,
			balances={Address(k): v for k, v in single_conf.get('balances', {}).items()},
			running_address=running_address,
		)

		host.balances.setdefault(running_address, 0)
		host.balances[running_address] += single_conf['message'].get('value', 0)

		# Get manager URI from the service
		manager_svc = self._test_case.manager_service
		port = manager_svc.meta['port']
		reroute_to = self._test_case.reroute_to
		manager_uri = f'http://localhost:{port}'

		# Run the test
		with host as mock_host:
			try:
				host_data = json.dumps(
					{
						'node_address': FAKE_NODE_ADDRESS,
						'tx_id': FAKE_TX_ID,
						'signerUrl': SIGNER_URL,
					}
				)
				request_extra = {}
				if 'stable' in self._test_case.description.tags:
					request_extra['no_modules'] = True
				case_permissions = single_conf.get('permissions')
				if case_permissions is not None:
					request_extra['permissions'] = case_permissions
				dflt_bucket = 2**200
				bucket_totals: list[int] = single_conf.get('bucket_totals', [dflt_bucket] * 10)

				default_message_fee_allocation = [
					fees.DEFAULT_EXTERNAL_MESSAGE_ALLOC,
					fees.DEFAULT_INTERNAL_FIN_MESSAGE_ALLOC,
					fees.DEFAULT_INTERNAL_ACC_MESSAGE_ALLOC,
				]

				message_fee_allocation: list[fees.MessageAllocationNode] = single_conf.get(
					'message_fee_allocation', default_message_fee_allocation
				)

				if not single_conf['message'].get('is_init', False):
					code = None
				mode = single_conf.get('mode', 'l')
				ctx = Context(logger)
				res = await base_host.run_genvm(
					mock_host,
					manager_uri=manager_uri,
					message=single_conf['message'],
					timeout=single_conf.get('deadline', 10 * 60),
					is_sync=(mode == 's'),
					host_data=host_data,
					ctx=ctx,
					host='unix://' + mock_host.path,
					debug_mode=self._debug_mode,
					code=code,
					calldata=calldata_bytes,
					bucket_totals=bucket_totals,
					gas_data=single_conf.get('gas_data'),
					leader_nondet_results=leader_nondet,
					message_fee_allocation=message_fee_allocation,
					reroute_to=reroute_to,
					request_extra=request_extra,
				)
				return_part = ''
				if res.result_kind == public_abi.ResultCode.RETURN:
					return_part += (
						f'executed with `Return({gvm_calldata.to_str(res.result_data)})`\n'
					)
				elif res.result_kind == public_abi.ResultCode.VM_ERROR:
					return_part += f'executed with `VMError("{res.result_data}")`\n'
				elif res.result_kind == public_abi.ResultCode.USER_ERROR:
					if isinstance(res.result_data, str):
						return_part += f'executed with `UserError("{res.result_data}")`\n'
					else:
						return_part += (
							f'executed with `UserError({gvm_calldata.to_str(res.result_data)})`\n'
						)
				nondet_part = ''
				if mock_host.nondet_disagreement_call_no is not None:
					nondet_part = (
						f'nondet disagreement: {mock_host.nondet_disagreement_call_no}\n'
					)

				for k, v in res.result_storage_changes:
					assert len(k) == 36
					index = int.from_bytes(k[32:], byteorder='big')
					index *= 32
					if index >= 4096:
						logger.warning(
							'suspicious storage writing', index=index, key=k.hex(), value=v.hex()
						)
					mock_host.storage.write(
						mock_host.running_address,
						k[:32],
						index,
						v,
					)
			except Exception as e:
				return {
					'passed': False,
					'context': {
						'exception': e,
						'reason': f'{e.__class__.__name__}: {e}',
						'tree_path': tree_path,
					},
				}

		# Save leader nondet results for v/s modes
		if is_leader:
			nondet_list = res.result_nondet_results

			with open(path_to_which_leader_puts_result, 'wb') as f:
				pickle.dump(nondet_list, f)

		# Save outputs
		my_tmp_dir.joinpath('stdout.txt').write_text(res.stdout)
		my_tmp_dir.joinpath('stderr.txt').write_text(res.stderr)
		with gzip.open(
			my_tmp_dir.joinpath('genvm.log.gz'), 'wt', compresslevel=5
		) as log_file:
			for log_line in res.genvm_log:
				json.dump(log_line, log_file)
				log_file.write('\n')

		# Save RunHostAndProgramRes pickle
		result_path = Path(single_conf['result_path'])
		result_path.parent.mkdir(exist_ok=True, parents=True)
		with open(result_path, 'wb') as f:
			pickle.dump(res, f)

		if res.result_kind == public_abi.ResultCode.INTERNAL_ERROR:
			return {
				'passed': False,
				'context': {
					'reason': 'internal error',
					'result_data': res.result_data,
					'stderr': res.stderr,
					'stdout': res.stdout,
					'genvm_log': res.genvm_log,
				},
			}

		result_events: list[list[bytes]] = []

		messages_content = []
		for em in res.result_emissions:
			if em['type'] == 'EmitEvent':
				tem = typing.cast(base_host.EmitEventInner, em)
				blob = gvm_calldata.encode(tem['blob'])
				result_events.append([*tem['topics'], blob])
			messages_content.append(gvm_calldata.to_str(em))
			messages_content.append('\n')

		# Write hash file (calldata-encoded deterministic result fields)
		hash_data = gvm_calldata.encode(
			[
				int(res.result_kind),
				res.result_data,
				res.result_fingerprint,
				res.result_storage_changes,
				result_events,
			]
		)

		semantics_parts = {
			'stdout': res.stdout,
			'return': return_part,
			'nondet': nondet_part,
			'kind': res.result_kind.name + '\n',
			'messages': ''.join(messages_content),
		}
		semantics_components = single_conf['expected_semantics_components']
		if semantics_components:
			semantics = ''.join(semantics_parts[c] for c in semantics_components)

			semantics_path = my_tmp_dir.joinpath('semantics.txt')
			semantics_path.write_text(semantics)

			exp_semantics_path = Path(single_conf['expected_semantics_path'])
			if exp_semantics_path.exists():
				exp_text = exp_semantics_path.read_text()
				diff = _get_diffs(exp_text, semantics, lambda x: x)
				if diff is not None:
					return {
						'passed': False,
						'context': {
							'reason': 'semantics mismatch',
							'expected_path': str(exp_semantics_path),
							'got_path': str(semantics_path),
							'semantics': semantics,
							'stderr': res.stderr,
							'genvm_log': res.genvm_log,
							**diff,
						},
					}
			else:
				# Create expected output file
				exp_semantics_path.write_text(semantics)

		# ADR-012 load-action log-grep assertions. A case may declare
		# `runner_load_asserts` on a step to assert on the executor's stable
		# `runner load` log records (charge counts, per-runner sizes, cached vs
		# charged); see origin.log_asserts for the schema. Gated on the main mode
		# (semantics_components is non-empty only there) so a nondet leader's
		# extra loads do not make validator/sandbox re-runs flaky.
		runner_load_asserts = single_conf.get('runner_load_asserts')
		if runner_load_asserts and semantics_components:
			load_errors = log_asserts.check(
				res.genvm_log,
				runner_load_asserts,
				code_len=(len(code) if code is not None else None),
			)
			if load_errors:
				return {
					'passed': False,
					'context': {
						'reason': 'runner load assertion failed',
						'tree_path': tree_path,
						'errors': load_errors,
						'runner_loads': log_asserts.summarize(res.genvm_log),
						'stderr': res.stderr,
						'genvm_log': res.genvm_log,
					},
				}

		my_tmp_dir.joinpath('hash').write_bytes(base64.b64encode(hash_data))

		expected_hash_path = single_conf['expected_hash_path']

		# A golden sidecar pins the hash across commits, and a line whose hashes
		# are still moving may legitimately stop tracking those. It may not stop
		# comparing this run's own modes against each other -- that is the
		# determinism property itself, and dropping it would leave a validator
		# step asserting only that it did not crash, since `expandModes` also
		# empties the semantics of every non-main mode. So when goldens are off,
		# fall back to the main mode's artifact rather than checking nothing.
		compares_golden = single_conf.get('expected_hash_is_golden', True)
		if compares_golden and (_is_ignore_hash_enabled() or not self._save_hashes):
			expected_hash_path = single_conf.get('runtime_hash_path')
			compares_golden = False

		check_expected_hash = expected_hash_path is not None
		if (
			res.result_kind == public_abi.ResultCode.VM_ERROR and res.result_data == 'timeout'
		):
			check_expected_hash = False  # Don't check hash for timeouts, as they can be flaky

		if check_expected_hash:
			expected_hash_path = Path(expected_hash_path)

			if expected_hash_path.exists():
				original_hash = base64.b64decode(expected_hash_path.read_text().strip())

				diff = _get_diffs(original_hash, hash_data, _calldata_to_fancy_str)

				if diff is not None:
					return {
						'passed': False,
						'context': {
							'reason': 'hash mismatch'
							if compares_golden
							else 'modes of one run disagree',
							'compared_against': 'committed golden'
							if compares_golden
							else 'this run of the main mode',
							'hash_path': str(expected_hash_path),
							'tree_path': tree_path,
							**diff,
						},
					}
			elif not compares_golden:
				# The main mode runs first and always writes its artifact, so a
				# missing one is a harness bug, not a golden waiting to be born.
				return {
					'passed': False,
					'context': {
						'reason': 'main mode produced no hash artifact',
						'hash_path': str(expected_hash_path),
						'tree_path': tree_path,
					},
				}
			else:
				expected_hash_path.parent.mkdir(exist_ok=True, parents=True)
				expected_hash_path.write_text(
					base64.b64encode(hash_data).decode('ascii') + '\n'
				)

		return {'passed': True, 'context': {'execution_time': res.execution_time}}


def _test_needs_webdriver(jsonnet_path: Path) -> bool:
	"""Check if a test needs webdriver based on its content."""
	content = jsonnet_path.read_text()
	# Web tests typically involve screenshots or webpage interactions
	return 'screenshot' in content.lower() or 'get_webpage' in content.lower()


def integration_test(
	ctx: genvm_tool.tests.stage.collection.Context,
	*,
	manager_service: genvm_tool.tests.stage.collection.Service,
	modules_service: genvm_tool.tests.stage.collection.Service,
	webdriver_service: genvm_tool.tests.stage.collection.Service,
	ci: bool,
) -> None:
	active_versions: list[str] = json.loads(
		local_ctx.shared.root_dir.joinpath('.genvm-monorepo-root').read_text()
	)['active-versions']
	for ver in active_versions:
		integration_test_single_executor(
			ctx,
			executor_version=ver,
			manager_service=manager_service,
			modules_service=modules_service,
			webdriver_service=webdriver_service,
			ci=ci,
		)


class RWLock:
	"""
	Many concurrent readers or a single writer, writers taking priority.

	A reader that arrives while a writer waits queues behind it, so a steady
	stream of lookups cannot starve a store.
	"""

	def __init__(self) -> None:
		self._cond = threading.Condition()
		self._readers = 0
		self._writer = False
		self._writers_waiting = 0

	@contextlib.contextmanager
	def read(self) -> typing.Iterator[None]:
		with self._cond:
			self._cond.wait_for(lambda: not self._writer and not self._writers_waiting)
			self._readers += 1
		try:
			yield
		finally:
			with self._cond:
				self._readers -= 1
				if self._readers == 0:
					self._cond.notify_all()

	@contextlib.contextmanager
	def write(self) -> typing.Iterator[None]:
		with self._cond:
			self._writers_waiting += 1
			try:
				self._cond.wait_for(lambda: not self._writer and not self._readers)
			finally:
				self._writers_waiting -= 1
			self._writer = True
		try:
			yield
		finally:
			with self._cond:
				self._writer = False
				self._cond.notify_all()


def _jsonnet_lib_dirs() -> list[str]:
	"""The search path a jsonnet vm starts with, in the order it is seeded."""
	return [
		f'/usr/share/jsonnet-{_jsonnet.version}/',
		f'/usr/local/share/jsonnet-{_jsonnet.version}/',
	]


def _digest_file(path: Path) -> str:
	"""Digest of ``path``'s contents, or '' when it is gone."""
	try:
		return hashlib.sha3_256(path.read_bytes()).hexdigest()
	except OSError:
		return ''


def _resolve_import(base: str, rel: str, jpathdir: list[str]) -> Path:
	"""Where jsonnet would find ``rel``, imported from a file in ``base``.

	Unresolved: a nested import resolves against this path.
	"""
	if os.path.isabs(rel):
		candidates = [Path(rel)]
	else:
		candidates = [Path(base) / rel]
		candidates.extend(Path(d) / rel for d in reversed(jpathdir))
		candidates.extend(Path(d) / rel for d in reversed(_jsonnet_lib_dirs()))
	for candidate in candidates:
		if candidate.is_file():
			return candidate
	raise RuntimeError(f'no such file to import: {rel} (from {base})')


class JsonnetCache:
	"""
	Memoizes jsonnet evaluation in a sqlite file among the test artifacts.

	Keyed by the case file's path and contents, carrying every import it read so
	that editing one of those misses too. Entries older than a day are dropped.
	"""

	MAX_AGE = 24 * 60 * 60
	COLUMNS = {'digest', 'stored_at', 'deps', 'result'}

	def __init__(self) -> None:
		# A native callback rewritten here, or a jsonnet upgrade, changes results
		# with no input file changing.
		self._evaluator_digest = hashlib.sha3_256(
			_jsonnet.version.encode('utf-8') + Path(__file__).read_bytes()
		).digest()

		artifacts_dir = local_ctx.shared.artifacts_dir
		artifacts_dir.mkdir(exist_ok=True, parents=True)
		self._path = artifacts_dir / 'jsonnet-cache.sqlite'
		self._lock = RWLock()
		# One connection per thread: a shared one would serialize the readers
		# that the lock lets run together.
		self._local = threading.local()

		with self._lock.write():
			db = self._connection()
			db.execute('PRAGMA journal_mode=WAL')
			columns = {
				row[1] for row in db.execute('PRAGMA table_info(evaluated)').fetchall()
			}
			if columns and columns != self.COLUMNS:
				db.execute('DROP TABLE evaluated')
			db.execute(
				'CREATE TABLE IF NOT EXISTS evaluated ('
				'digest BLOB PRIMARY KEY, stored_at REAL NOT NULL, '
				'deps TEXT NOT NULL, result TEXT NOT NULL'
				') WITHOUT ROWID'
			)
			db.execute(
				'DELETE FROM evaluated WHERE stored_at < ?', (time.time() - self.MAX_AGE,)
			)
			db.commit()

	def _connection(self) -> sqlite3.Connection:
		db = getattr(self._local, 'db', None)
		if db is None:
			db = sqlite3.connect(self._path)
			# Per connection, unlike the journal mode. Losing the tail of a cache
			# to a power cut costs a re-evaluation.
			db.execute('PRAGMA synchronous=NORMAL')
			self._local.db = db
		return db

	@staticmethod
	def _deps_hold(deps: list[list[str]], jpathdir: list[str]) -> bool:
		"""Whether every import still resolves where it did, to the same bytes.

		Re-resolved, not just re-digested: a new file of that name beside the
		case steals an import that fell through to the search path.
		"""
		for base, rel, found, digest in deps:
			try:
				if str(_resolve_import(base, rel, jpathdir)) != found:
					return False
			except RuntimeError:
				return False
			if _digest_file(Path(found)) != digest:
				return False
		return True

	def eval(self, path: Path, *args, jpathdir: list[str], **kwargs) -> str:
		# Keyed on the path too: two identical cases in different directories
		# import different files under the same relative names.
		full_digest = hashlib.sha3_256(
			self._evaluator_digest
			+ str(path.resolve()).encode('utf-8')
			+ b'\0'
			+ path.read_bytes()
		).digest()

		eval_start = time.monotonic()

		with self._lock.read():
			row = (
				self._connection()
				.execute('SELECT deps, result FROM evaluated WHERE digest = ?', (full_digest,))
				.fetchone()
			)
		cache_check_end = time.monotonic()
		local_ctx.shared.bump_metric(
			'jsonnet_cache_check_time', 0.0, cache_check_end - eval_start
		)

		if row is not None and self._deps_hold(json.loads(row[0]), jpathdir):
			return row[1]

		deps: list[list[str]] = []

		def import_callback(base: str, rel: str) -> tuple[str, bytes]:
			found = _resolve_import(base, rel, jpathdir)
			content = found.read_bytes()
			deps.append([base, rel, str(found), hashlib.sha3_256(content).hexdigest()])
			return str(found), content

		# Replaces the importer wholesale, `jpathdir` included.
		result = _jsonnet.evaluate_file(
			str(path),
			*args,
			import_callback=import_callback,
			**kwargs,
		)

		eval_end = time.monotonic()
		local_ctx.shared.bump_metric('jsonnet_eval_time', 0.0, eval_end - cache_check_end)

		with self._lock.write():
			db = self._connection()
			db.execute(
				'INSERT OR REPLACE INTO evaluated (digest, stored_at, deps, result) '
				'VALUES (?, ?, ?, ?)',
				(full_digest, time.time(), json.dumps(deps), result),
			)
			db.commit()

		write_end = time.monotonic()
		local_ctx.shared.bump_metric('jsonnet_cache_write_time', 0.0, write_end - eval_end)

		return result


def integration_test_single_executor(
	ctx: genvm_tool.tests.stage.collection.Context,
	*,
	executor_version: str,
	manager_service: genvm_tool.tests.stage.collection.Service,
	modules_service: genvm_tool.tests.stage.collection.Service,
	webdriver_service: genvm_tool.tests.stage.collection.Service,
	ci: bool,
) -> None:
	"""
	Collect integration tests from tests/integration/ directory.

	Each jsonnet file produces multiple test cases:
		- <name>/prepare  — setup (temp dir, prepare script, base storage)
		- <name>/<tree_path><mode> — one per step (e.g. /0l, /0v, /0s)

	Steps depend on /prepare, and child steps depend on their parent step.
	"""

	import _jsonnet

	globals()['_jsonnet'] = _jsonnet  # type: ignore

	import genvm_tool.common

	EXEC_SUBDIR = os.environ.get(
		'GENVM_TEST_EXEC_SUBDIR', f'executors/{executor_version}.x'
	)
	executor_root = local_ctx.shared.root_dir / EXEC_SUBDIR
	CASES_DIR = executor_root / 'tests' / 'integration'

	# Run this line's cases against this line's own executor. The version key
	# (e.g. "v0.2") maps to the concrete built version (e.g. "v0.2.16").
	reroute_to = build_info['executor_versions'].get(executor_version, executor_version)

	executor_project = genvm_tool.common.load_project(executor_root)
	executor_integration_fn = getattr(executor_project, 'integration', None)
	executor_integration_conf: dict = (
		executor_integration_fn() if executor_integration_fn else {}
	)
	# `save-hashes` replaces the inverted `ignore-hash`; a line that has not been
	# renamed yet still gets what it asked for.
	save_hashes = executor_integration_conf.get(
		'save-hashes', not executor_integration_conf.get('ignore-hash', False)
	)

	glob_start = time.monotonic()
	jsonnet_files = list(
		x for x in CASES_DIR.glob('**/*.jsonnet') if not x.name.startswith('_')
	)
	glob_end = time.monotonic()
	# Bumped, not assigned: this runs once per executor line.
	local_ctx.shared.bump_metric('jsonnet_glob_time', 0.0, glob_end - glob_start)
	jsonnet_files.sort()

	from concurrent.futures import ThreadPoolExecutor, as_completed

	jsonnet_cache = JsonnetCache()

	with ThreadPoolExecutor(thread_name_prefix='integration-test-collector') as executor:
		futures = {
			executor.submit(
				_single_integration_test,
				ctx,
				jsonnet_file,
				jsonnet_cache,
				cases_dir=CASES_DIR,
				reroute_to=reroute_to,
				save_hashes=save_hashes,
				manager_service=manager_service,
				modules_service=modules_service,
				webdriver_service=webdriver_service,
				ci=ci,
			): jsonnet_file
			for jsonnet_file in jsonnet_files
		}
		for future in as_completed(futures):
			future.result()


def _gvm32_of_str(text: str) -> str:
	"""Hash `text` the way the executor names content: gvm32(sha3_256(utf-8))."""
	return genvm_tool.gvm32.encode(hashlib.sha3_256(text.encode('utf-8')).digest())


def _single_integration_test(
	ctx: genvm_tool.tests.stage.collection.Context,
	jsonnet_file: Path,
	jsonnet_cache: JsonnetCache,
	*,
	cases_dir: Path,
	reroute_to: str,
	save_hashes: bool,
	manager_service: genvm_tool.tests.stage.collection.Service,
	modules_service: genvm_tool.tests.stage.collection.Service,
	webdriver_service: genvm_tool.tests.stage.collection.Service,
	ci: bool,
) -> None:
	rel_path = jsonnet_file.relative_to(cases_dir)

	# Determine stability tag from path
	tags: set[str] = {'integration'}
	stability_tag = rel_path.parts[0] if rel_path.parts else 'unknown'
	if stability_tag in ('stable', 'unstable', 'semi-stable'):
		tags.add(stability_tag)
	elif stability_tag.startswith('_'):
		tags.add('stable')
		stability_tag = 'stable'

	needed_services: set[genvm_tool.tests.stage.collection.Service] = {manager_service}

	if 'stable' not in tags:
		needed_services.add(modules_service)
		needed_services.add(webdriver_service)

	test_name = str(jsonnet_file.relative_to(local_ctx.shared.root_dir))

	# Handle skipped tests
	if jsonnet_file.with_suffix('.skip').exists():
		desc = genvm_tool.tests.test.Description(
			name=test_name,
			needed_services=frozenset(needed_services),
			tags=frozenset(tags),
		)
		ctx.add_case(
			genvm_tool.tests.test.StepsCase(
				description=desc,
				steps=[IntegrationSkipStep(test_name)],
			)
		)
		return

	# Parse jsonnet at collection time.
	#
	# `native_callbacks` exposes helpers to jsonnet as `std.native('<name>')`.
	# Note the binding only marshals *primitives* — passing an array or object
	# to a native function is a jsonnet runtime error, so anything structured
	# has to cross as a JSON string.
	jsonnet_result = jsonnet_cache.eval(
		jsonnet_file,
		jpathdir=[str(TEMPLATES_DIR.parent)],
		native_callbacks={
			# std.native('gvm32')('...') -> gvm32(sha3_256(utf8(s))), the id form
			# used by content-addressed names such as `custom:<hash>`.
			'gvm32': (('text',), _gvm32_of_str),
		},
	)

	jsonnet_parsed = json.loads(jsonnet_result)
	extra_tags = jsonnet_parsed.get('tags', [])
	if extra_tags:
		tags.update(extra_tags)

	debug_mode = jsonnet_parsed.get('debug_mode', _DEBUG_MODE_DEFAULT)
	if debug_mode not in _DEBUG_MODES:
		raise ValueError(
			f'{jsonnet_file}: unknown debug_mode {debug_mode!r}, expected one of {_DEBUG_MODES}'
		)
	if debug_mode == 'disabled':
		# `reroute_to` is honored only from `safe` up, and that is what points a
		# case at its own line's executor. Below it the case would silently run
		# whatever the manifest resolves to.
		raise ValueError(f'{jsonnet_file}: debug_mode "disabled" cannot reroute')

	# Recompute needed_services after tags update
	needed_services = {manager_service}
	if 'stable' not in tags:
		needed_services.add(modules_service)
		needed_services.add(webdriver_service)

	# Compute tmp_dir and unfold config. Each step's artifacts share that step's
	# per-test case directory (<artifacts>/cases/<test_name>/<tree_path>/), with
	# shared storage living at the test root (<artifacts>/cases/<test_name>/).
	tmp_dir = local_ctx.shared.case_dir_for(test_name)

	jsonnet_conf = _unfold_conf(
		jsonnet_parsed,
		{
			'jsonnetDir': str(jsonnet_file.parent),
			'fileBaseName': jsonnet_file.stem,
			'tmpDir': str(tmp_dir),
		},
	)

	top_level_conf = {
		k: v for k, v in jsonnet_conf.items() if k not in _TOP_LEVEL_METADATA_KEYS
	}
	entries = jsonnet_conf['entry']
	flat_steps = _flatten_tree(entries)

	is_unstable = 'unstable' in tags
	max_attempts = 3 if is_unstable else 1

	frozen_services = frozenset(needed_services)
	frozen_tags = frozenset(tags)

	# Create prepare case
	prepare_name = f'{test_name}/prepare'
	prepare_desc = genvm_tool.tests.test.Description(
		name=prepare_name,
		needed_services=frozen_services,
		tags=frozen_tags,
	)
	ctx.add_case(
		IntegrationPrepareCase(
			description=prepare_desc,
			jsonnet_path=jsonnet_file,
			top_level_conf=top_level_conf,
			tmp_dir=tmp_dir,
			ci=ci,
		)
	)

	# Build tree_path -> step name map
	step_names: dict[str, str] = {}
	for tree_path, _dep, single_conf in flat_steps:
		step_names[tree_path] = f'{test_name}/{tree_path}'

	# Create one test case per step
	total_steps = len(flat_steps)
	for tree_path, depends_on_tree_path, single_conf in flat_steps:
		step_name = step_names[tree_path]
		is_benchmark = single_conf.get('benchmark', False)

		if depends_on_tree_path is None:
			deps = frozenset([prepare_name])
		else:
			deps = frozenset([step_names[depends_on_tree_path]])

		step_desc = genvm_tool.tests.test.Description(
			name=step_name,
			needed_services=frozen_services,
			tags=frozen_tags,
			depends_on=deps,
		)

		# parent_tree_path from step_conf (original jsonnet base path,
		# e.g. "0") is needed for storage path resolution in _run_single_step
		ctx.add_case(
			IntegrationSingleCase(
				description=step_desc,
				jsonnet_path=jsonnet_file,
				cases_dir=cases_dir,
				reroute_to=reroute_to,
				manager_service=manager_service,
				tree_path=tree_path,
				parent_tree_path=single_conf.get('parent_tree_path'),
				single_conf=single_conf,
				total_steps=total_steps,
				tmp_dir=tmp_dir,
				max_attempts=max_attempts,
				debug_mode=debug_mode,
				save_hashes=save_hashes,
				is_benchmark=is_benchmark,
				ci=ci,
			)
		)
