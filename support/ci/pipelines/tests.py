import argparse
import json
import os
import subprocess
import typing
from dataclasses import dataclass, field

import ci_lib
from pipelines.build import build_and_pack

type SubStep = dict[str, typing.Any]

type GithubStep = list[SubStep]

type MultiGithubStep = dict[str, GithubStep]


def merge_with_none[T](
	l: T | None, r: T | None, fn: typing.Callable[[T, T], T]
) -> T | None:
	if l is None:
		return r
	if r is None:
		return l
	return fn(l, r)


def merge_str_bar(l: str, r: str) -> str:
	return f'({l})|({r})'


# --- step builders -------------------------------------------------------------
# A cell's work is a JSON list of steps that TestMatrixCell replays. The shapes
# below match the ci_lib call each step maps to, so keep them in sync with
# TestMatrixCell.handler.


def step_run(
	command: list[str], *, check: bool = True, expect_exit_codes: list[int] | None = None
) -> SubStep:
	ret = {'type': 'run', 'command': command, 'check': check}
	if expect_exit_codes is not None:
		ret['expect_exit_codes'] = expect_exit_codes
	return ret


def step_nix_develop(
	installable: str,
	command: list[str],
	*,
	check: bool = True,
	subcommand_group: str | None = None,
	expect_exit_codes: list[int] | None = None,
) -> SubStep:
	ret = {
		'type': 'nix-develop',
		'installable': installable,
		'command': command,
		'check': check,
	}
	if subcommand_group is not None:
		ret['subcommand_group'] = subcommand_group
	if expect_exit_codes is not None:
		ret['expect_exit_codes'] = expect_exit_codes
	return ret


def _check_of(step: SubStep) -> bool:
	# A step asserting exact exit codes must survive a non-zero one to assert on
	# it, so `expect_exit_codes` implies check=False and _finish does the judging.
	if step.get('expect_exit_codes') is not None:
		return False
	return step.get('check', True)


def _finish(step: SubStep, result: subprocess.CompletedProcess) -> None:
	"""Enforce a step's `expect_exit_codes`, if it declared any.

	Lets a step assert an exact non-zero exit — e.g. a daemon smoke test killed by
	`timeout` must exit 124 — without a shell to write `|| test $? -eq 124` in.
	`check` is what tolerates *any* failure; this tolerates a named set.
	"""
	expected = step.get('expect_exit_codes')
	if expected is None:
		return
	if result.returncode not in expected:
		raise ValueError(
			f'{step["command"]} exited {result.returncode}, expected one of {expected}'
		)


@dataclass
class ToolTest:
	tests_name_filter_for_or: str | None
	tests_tags_filter_for_or: str | None

	def merge(self, r: 'ToolTest') -> 'ToolTest':
		return ToolTest(
			tests_name_filter_for_or=merge_with_none(
				self.tests_name_filter_for_or, r.tests_name_filter_for_or, merge_str_bar
			),
			tests_tags_filter_for_or=merge_with_none(
				self.tests_tags_filter_for_or, r.tests_tags_filter_for_or, merge_str_bar
			),
		)


@dataclass
class Stage[T]:
	data: T
	commands_pre: GithubStep = field(default_factory=list)
	commands_post: GithubStep = field(default_factory=list)

	def merge(self, r: 'Stage[T]', fn: typing.Callable[[T, T], T]) -> 'Stage[T]':
		return Stage(
			commands_pre=self.commands_pre + r.commands_pre,
			commands_post=self.commands_post + r.commands_post,
			data=fn(self.data, r.data),
		)


@dataclass
class DefaultStepInfo:
	configure_step: Stage[None] | None = None
	build_step: Stage[frozenset[str]] | None = None
	tool_step: Stage[ToolTest] | None = None

	def merge(self, r: 'DefaultStepInfo') -> 'DefaultStepInfo':
		return DefaultStepInfo(
			configure_step=merge_with_none(
				self.configure_step,
				r.configure_step,
				lambda a, b: a.merge(b, lambda _l, _r: None),
			),
			build_step=merge_with_none(
				self.build_step, r.build_step, lambda a, b: a.merge(b, lambda l, r: l | r)
			),
			tool_step=merge_with_none(
				self.tool_step, r.tool_step, lambda a, b: a.merge(b, ToolTest.merge)
			),
		)

	def into_commands(self) -> MultiGithubStep:
		multi_step: MultiGithubStep = {
			'configure': [],
			'build': [],
			'tool': [],
		}

		def extend_step(gh_step: str, stage: Stage, body: GithubStep) -> None:
			multi_step[gh_step].extend(stage.commands_pre)
			multi_step[gh_step].extend(body)
			multi_step[gh_step].extend(stage.commands_post)

		def group(gh_step: str, name: str, stage: Stage, body: GithubStep) -> None:
			multi_step[gh_step].append({'type': 'group-start', 'name': name})
			extend_step(gh_step, stage, body)
			multi_step[gh_step].append({'type': 'group-end'})

		if (stage := self.configure_step) is not None:
			extend_step(
				'configure',
				stage,
				[
					step_nix_develop(
						'.?submodules=1#minimal', ['genvm-tool', 'configure', '--ci']
					)
				],
			)

		if (stage := self.build_step) is not None:
			multi_step['build'].extend(stage.commands_pre)
			for subtarget in list(sorted(stage.data)):
				multi_step['build'].append(
					{'type': 'group-start', 'name': f'build {subtarget}'}
				)
				multi_step['build'].append(
					step_nix_develop(
						'.?submodules=1#rust-test',
						['ninja', '--verbose', '-C', 'build', subtarget],
					)
				)
				multi_step['build'].append({'type': 'group-end'})
			multi_step['build'].extend(stage.commands_post)

		if (stage := self.tool_step) is not None:
			filters: list[str] = []
			if stage.data.tests_name_filter_for_or is not None:
				filters += ['--filter-name', stage.data.tests_name_filter_for_or]
			if stage.data.tests_tags_filter_for_or is not None:
				filters += ['--filter-tag', stage.data.tests_tags_filter_for_or]
			group(
				'tool',
				f'tool {stage.data.tests_name_filter_for_or or ""}',
				stage,
				[
					step_nix_develop(
						'.?submodules=1#mock-tests', ['genvm-tool', 'test', 'run', '--ci', *filters]
					)
				],
			)

		return multi_step


# in future we can shard it
TEST_RUST_EXECUTORS = DefaultStepInfo(
	tool_step=Stage(
		data=ToolTest(
			tests_name_filter_for_or='^executors/',
			tests_tags_filter_for_or='rust',
		),
	),
)

TEST_RUST_MANAGER = DefaultStepInfo(
	configure_step=Stage(None),
	tool_step=Stage(
		data=ToolTest(
			tests_name_filter_for_or='^(?!executors/)',
			tests_tags_filter_for_or='rust',
		),
	),
)

TEST_PYTHON = DefaultStepInfo(
	tool_step=Stage(
		data=ToolTest(
			tests_name_filter_for_or=None,
			tests_tags_filter_for_or='python',
		),
	)
)

TEST_MOCK_BUILD_BINS = DefaultStepInfo(
	configure_step=Stage(None),
	build_step=Stage(data=frozenset(['all/bin'])),
)

TEST_MOCK_BUILD_RUNNERS = DefaultStepInfo(
	configure_step=Stage(None),
	build_step=Stage(data=frozenset(['all/runners', 'all/data'])),
)

TEST_MOCK_BUILD_ALL = TEST_MOCK_BUILD_BINS.merge(TEST_MOCK_BUILD_RUNNERS)

TEST_MOCK_PR = DefaultStepInfo(
	tool_step=Stage(
		commands_pre=[
			{'type': 'group-start', 'name': 'precompile'},
			step_run(['./build/out/bin/genvm-manager', 'check-install', '--precompile']),
			{'type': 'group-end'},
		],
		data=ToolTest(
			tests_name_filter_for_or=None,
			tests_tags_filter_for_or='integration',
		),
	)
)

TEST_MOCK_RELEASE = DefaultStepInfo(
	tool_step=Stage(
		commands_pre=[
			step_run(['./build/out/bin/genvm-manager', 'check-install', '--precompile'])
		],
		data=ToolTest(
			tests_name_filter_for_or=None,
			tests_tags_filter_for_or='integration & stable & !bench',
		),
	)
)


def _cell(
	name: str,
	info: DefaultStepInfo,
	*,
	runs_on: str = 'ubuntu-latest',
	disk_reclaim: bool = False,
	fuzz_host: bool = False,
	buildx: bool = False,
	gcp: bool = False,
) -> dict:
	# `name` labels the cell in the UI; `job_json` is the step list TestMatrixCell
	# replays. The booleans drive test_cell.yaml's setup steps — every key is always
	# present because matrix-tests passes each to a typed reusable-workflow input.
	return {
		'name': name,
		'job_json': json.dumps(info.into_commands()),
		'runs_on': runs_on,
		'disk_reclaim': disk_reclaim,
		'fuzz_host': fuzz_host,
		'buildx': buildx,
		'gcp': gcp,
	}


QUEUE_CELLS = [
	_cell('python', TEST_PYTHON),
	_cell('rust-executors', TEST_RUST_EXECUTORS, fuzz_host=True),
	_cell('rust-manager', TEST_RUST_MANAGER, fuzz_host=True),
	_cell(
		'mock-pr',
		TEST_MOCK_BUILD_ALL.merge(TEST_MOCK_PR),
		buildx=True,
		disk_reclaim=True,
		fuzz_host=True,
		gcp=True,
	),
]


class PlanQueueMatrix(ci_lib.Pipeline):
	"""Emit queue.yaml's test matrix as a GitHub `matrix` output.

	The heavy cells run only when a marker is set (RUN_FULL_TESTS, from the rtm /
	run-full-tests labels or a manual dispatch); otherwise the matrix is empty and
	matrix-tests is skipped.
	"""

	def name(self) -> str:
		return 'plan-queue-matrix'

	def handler(self, args: argparse.Namespace) -> int:
		run_full = os.environ.get('RUN_FULL_TESTS', '').strip().lower() == 'true'
		matrix = json.dumps({'include': QUEUE_CELLS if run_full else []})
		out = os.environ.get('GITHUB_OUTPUT')
		if not out:
			print(f'GITHUB_OUTPUT not set; matrix would be:\n{matrix}')
			return 1
		with open(out, 'a') as f:
			f.write(f'matrix={matrix}\n')
		print(f'planned matrix (RUN_FULL_TESTS={run_full}): {matrix}')
		return 0


class TestMatrixCell(ci_lib.Pipeline):
	"""Replay one step list of a matrix cell (from plan-queue-matrix / into_commands).

	into_commands emits a list per key ('configure', 'build', 'tool'); test_cell.yaml
	slices it with fromJSON and hands us one of those lists, so --job-json here is a
	flat step list, not the whole mapping. The runner-side setup (disk reclaim, fuzz
	host, buildx, gcp) is done by test_cell.yaml before this runs.
	"""

	def name(self) -> str:
		return 'test-cell'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		parser.add_argument('--job-json', required=True, type=str)

	def handler(self, args: argparse.Namespace) -> int:
		all_steps: GithubStep = json.loads(args.job_json)

		nix_develop_installables = set()
		for step in all_steps:
			if step['type'] == 'nix-develop':
				nix_develop_installables.add(step['installable'])

		if len(nix_develop_installables) > 1:
			all_installables = list(sorted(nix_develop_installables))
			with ci_lib.github_group('nix-develop installables pre-cache'):
				for inst in all_installables:
					ci_lib.nix_develop_pre_cache(inst, add_group=False)
		for step in all_steps:
			match step['type']:
				case 'group-start':
					ci_lib.github_group_start(step['name'])
				case 'group-end':
					ci_lib.github_group_end()
				case 'run':
					_finish(
						step,
						ci_lib.run(step['command'], check=_check_of(step)),
					)
				case 'nix-develop':
					_finish(
						step,
						ci_lib.nix_develop(
							step['installable'],
							step['command'],
							check=_check_of(step),
							subcommand_group=step.get('subcommand_group'),
						),
					)
				case 'build-and-pack':
					build_and_pack(
						installable=step['installable'],
						artifact_name=step['artifact_name'],
						globs=step.get('globs'),
						compression_level=step.get('compression_level', 9),
						show_trace=step.get('show_trace', False),
					)
				case other:
					raise ValueError(f'unknown step type: {other!r}')
		return 0


class CheckCellKeys(ci_lib.Pipeline):
	"""Fail unless a cell's step mapping holds exactly the keys the caller replays.

	into_commands' keys and test_cell.yaml's "Job:" steps must stay in sync in both
	directions: a key nobody replays would silently drop its steps, and a step whose
	key is gone would silently do nothing. Neither fails on its own, so the workflow
	passes the keys it handles and we require an exact match.
	"""

	def name(self) -> str:
		return 'check-cell-keys'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		parser.add_argument('--job-json', required=True, type=str)
		parser.add_argument('keys', nargs='+', help='keys the caller replays')

	def handler(self, args: argparse.Namespace) -> int:
		multi_step = json.loads(args.job_json)
		if not isinstance(multi_step, dict):
			ci_lib.github_error(
				f'job_json must be a mapping of key to step list, got {type(multi_step).__name__}'
			)
			return 1

		expected = set(args.keys)
		actual = set(multi_step.keys())
		for key in sorted(actual - expected):
			ci_lib.github_error(
				f'job_json key {key!r} is replayed by no step: add one to test_cell.yaml or drop the key from into_commands'
			)
		for key in sorted(expected - actual):
			ci_lib.github_error(
				f'no job_json key {key!r}: drop its step from test_cell.yaml or emit the key from into_commands'
			)
		if actual != expected:
			return 1

		print(f'cell keys are in sync: {sorted(actual)}')
		return 0


COMMANDS = [
	PlanQueueMatrix(),
	TestMatrixCell(),
	CheckCellKeys(),
]
