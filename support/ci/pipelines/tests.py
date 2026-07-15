import argparse
import json
import os
import typing
from dataclasses import dataclass, field

import ci_lib


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


def step_run(command: list[str], *, check: bool = True) -> dict:
	return {'type': 'run', 'command': command, 'check': check}


def step_nix_develop(
	installable: str,
	command: list[str],
	*,
	check: bool = True,
	subcommand_group: str | None = None,
) -> dict:
	return {
		'type': 'nix-develop',
		'installable': installable,
		'command': command,
		'check': check,
		'subcommand_group': subcommand_group,
	}


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
	commands_pre: list[dict] = field(default_factory=list)
	commands_post: list[dict] = field(default_factory=list)

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

	def into_commands(self) -> list[dict]:
		steps: list[dict] = []

		def group(name: str, stage: Stage, body: list[dict]) -> None:
			steps.append({'type': 'group-start', 'name': name})
			steps.extend(stage.commands_pre)
			steps.extend(body)
			steps.extend(stage.commands_post)
			steps.append({'type': 'group-end'})

		if (stage := self.configure_step) is not None:
			group(
				'configure',
				stage,
				[step_nix_develop('.?submodules=1#minimal', ['genvm-tool', 'configure'])],
			)

		if (stage := self.build_step) is not None:
			group(
				'build',
				stage,
				[
					step_nix_develop(
						'.?submodules=1#rust-test',
						['ninja', '--verbose', '-C', 'build', *sorted(stage.data)],
					)
				],
			)

		if (stage := self.tool_step) is not None:
			filters: list[str] = []
			if stage.data.tests_name_filter_for_or is not None:
				filters += ['--filter-name', stage.data.tests_name_filter_for_or]
			if stage.data.tests_tags_filter_for_or is not None:
				filters += ['--filter-tag', stage.data.tests_tags_filter_for_or]
			group(
				'test',
				stage,
				[
					step_nix_develop(
						'.?submodules=1#mock-tests', ['genvm-tool', 'test', 'run', '--ci', *filters]
					)
				],
			)

		return steps


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
	"""Replay one matrix cell's step list (from plan-queue-matrix / into_commands).

	The runner-side setup (disk reclaim, fuzz host, buildx, gcp) is done by
	test_cell.yaml before this runs; here we just execute the steps.
	"""

	def name(self) -> str:
		return 'test-cell'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		parser.add_argument('--job-json', required=True, type=str)

	def handler(self, args: argparse.Namespace) -> int:
		for step in json.loads(args.job_json):
			match step['type']:
				case 'group-start':
					ci_lib.github_group_start(step['name'])
				case 'group-end':
					ci_lib.github_group_end()
				case 'run':
					ci_lib.run(step['command'], check=step.get('check', True))
				case 'nix-develop':
					ci_lib.nix_develop(
						step['installable'],
						step['command'],
						check=step.get('check', True),
						subcommand_group=step.get('subcommand_group'),
					)
				case other:
					raise ValueError(f'unknown step type: {other!r}')
		return 0


COMMANDS = [
	PlanQueueMatrix(),
	TestMatrixCell(),
]
