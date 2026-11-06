"""
`genvm-manager check-install` against a patched install.

The case edits the shared install, so it runs alone in the console pool and
restores the original file from a backup afterwards.
"""

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

import genvm_tool.tests
from genvm_tool.tests.test import Result

UNDEFINED_FORK = 'genvm/executor/v0.3/undefined'


async def _run_check_install(build_dir: Path) -> tuple[int, str]:
	proc = await asyncio.create_subprocess_exec(
		str(build_dir / 'out' / 'bin' / 'genvm-manager'),
		'check-install',
		stdout=asyncio.subprocess.PIPE,
		stderr=asyncio.subprocess.STDOUT,
	)
	output, _ = await proc.communicate()
	return proc.returncode, output.decode(errors='replace')


def _add_undefined_runner_fork_case(
	ctx: genvm_tool.tests.stage.collection.Context, build_dir: Path, executor: str
) -> None:
	registry = build_dir / 'out' / 'executor' / executor / 'data' / 'all.json'
	backup = registry.with_name('all.json.check-install-backup')

	async def run(previous_results):
		start = time.monotonic()
		shutil.copy2(registry, backup)
		try:
			doc = json.loads(registry.read_text())
			runner_id, entry = next(iter(doc.items()))
			runner_hash, version = next(iter(entry['versions'].items()))
			version['forks'] += f'|{UNDEFINED_FORK}'
			registry.write_text(json.dumps(doc))

			exit_code, output = await _run_check_install(build_dir)
		finally:
			os.replace(backup, registry)

		expected = f'runner {runner_id}:{runner_hash} names fork `{UNDEFINED_FORK}`'
		return Result(
			passed=exit_code != 0 and expected in output,
			context={'exit_code': exit_code, 'expected': expected, 'output': output},
			elapsed_seconds=time.monotonic() - start,
		)

	desc = genvm_tool.tests.test.Description(
		name='tests/system/check-install/undefined-runner-fork',
		tags=frozenset({'integration', 'stable', 'feature-check-install'}),
		console_pool=True,
	)
	ctx.add_case(
		genvm_tool.tests.test.StepsCase(
			description=desc, steps=[genvm_tool.tests.exec.step.PythonFunction(run)]
		)
	)


def collect(ctx: genvm_tool.tests.stage.collection.Context) -> None:
	build_info = json.loads(
		ctx.shared.root_dir.joinpath('build', 'info.json').read_text()
	)
	_add_undefined_runner_fork_case(
		ctx, Path(build_info['build_dir']), build_info['executor_versions']['v0.3']
	)
