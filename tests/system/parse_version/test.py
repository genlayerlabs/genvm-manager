"""parse-version-pattern system tests.

Each ``*.wat`` here is compiled to wasm and piped into
``genvm parse-version-pattern``; its ``*.expected`` sibling holds the expected
stdout. Evaluated by ``collection.Context.collect_dir`` — ``collect`` registers
one case per ``.wat`` file.
"""

import json
import os
import shlex
from pathlib import Path

import genvm_tool.tests
from genvm_tool.tests.test import Result


def _default_env() -> dict[str, str]:
	return {
		k: v
		for k, v in os.environ.items()
		if genvm_tool.tests.util.environ.DEFAULT_FILTER(k, v)
	}


def _add_case(
	ctx: genvm_tool.tests.stage.collection.Context,
	*,
	wat_file: Path,
	expected_file: Path,
	genvm_bin: Path,
	config_path: Path,
	artifacts_dir: Path,
) -> None:
	name = f'tests/system/parse_version/{wat_file.stem}'
	desc = genvm_tool.tests.test.Description(name).with_tags(['parse_version'])

	test_env = _default_env()

	tmp_dir = artifacts_dir / wat_file.stem
	wasm_file = tmp_dir / f'{wat_file.stem}.wasm'

	expected_output = expected_file.read_text().strip()

	steps: list[genvm_tool.tests.exec.step.Step] = []
	steps.append(genvm_tool.tests.exec.step.MkDir(path=tmp_dir))
	steps.append(genvm_tool.tests.exec.step.SetCwd(path=tmp_dir))

	for k, v in test_env.items():
		steps.append(genvm_tool.tests.exec.step.SetEnv(key=k, value=v))

	# Compile WAT to WASM
	steps.append(
		genvm_tool.tests.exec.step.Run(
			args=[
				'wat2wasm',
				'--enable-annotations',
				'-o',
				str(wasm_file),
				str(wat_file),
			],
			mode=genvm_tool.tests.exec.command.RunMode.SILENT,
		)
	)
	steps.append(genvm_tool.tests.test.CommandToResultStep())
	steps.append(genvm_tool.tests.test.ResultStopIfErrorStep())

	# Run genvm parse-version-pattern with wasm piped to stdin
	shell_cmd = (
		f'{shlex.quote(str(genvm_bin))}'
		f' --config {shlex.quote(str(config_path))}'
		f' parse-version-pattern'
		f' < {shlex.quote(str(wasm_file))}'
	)
	steps.append(
		genvm_tool.tests.exec.step.Run(
			args=['sh', '-c', shell_cmd],
			mode=genvm_tool.tests.exec.command.RunMode.SILENT,
		)
	)

	# Validate output
	async def validate(previous_results):
		res = previous_results[-1]
		assert isinstance(res, genvm_tool.tests.exec.command.Result)
		actual = res.stdout
		if res.exit_code != 0:
			return Result(
				passed=False,
				context={
					'reason': 'genvm parse-version-pattern failed',
					'exit_code': res.exit_code,
					'stderr': res.stderr,
				},
				elapsed_seconds=res.elapsed_seconds,
			)
		if actual != expected_output:
			return Result(
				passed=False,
				context={
					'expected': repr(expected_output),
					'actual': repr(actual),
				},
				elapsed_seconds=res.elapsed_seconds,
			)
		return Result(passed=True, context={}, elapsed_seconds=res.elapsed_seconds)

	steps.append(genvm_tool.tests.exec.step.PythonFunction(validate))

	ctx.add_case(genvm_tool.tests.test.StepsCase(description=desc, steps=steps))


def collect(ctx: genvm_tool.tests.stage.collection.Context) -> None:
	build_info = json.loads(
		ctx.shared.root_dir.joinpath('build', 'info.json').read_text()
	)
	build_dir = Path(build_info['build_dir'])

	reroute_to = (
		getattr(ctx.configuration.args, 'genvm_reroute_to', '')
		or build_info['primary_executor_version']
	)
	genvm_bin = build_dir / 'out' / 'executor' / reroute_to / 'bin' / 'genvm'
	config_path = build_dir / 'out' / 'executor' / reroute_to / 'config' / 'genvm.yaml'

	artifacts_dir = ctx.shared.artifacts_dir / 'parse_version'
	artifacts_dir.mkdir(parents=True, exist_ok=True)

	test_dir = ctx.shared.root_dir / 'tests' / 'system' / 'parse_version'
	for wat_file in sorted(test_dir.glob('*.wat')):
		expected_file = wat_file.with_suffix('.expected')
		if not expected_file.exists():
			ctx.shared.logger.warning(
				'no .expected file for WAT test', wat_file=str(wat_file)
			)
			continue
		_add_case(
			ctx,
			wat_file=wat_file,
			expected_file=expected_file,
			genvm_bin=genvm_bin,
			config_path=config_path,
			artifacts_dir=artifacts_dir,
		)
