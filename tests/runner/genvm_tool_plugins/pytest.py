import collections.abc
import os
import shlex
from pathlib import Path

import genvm_tool.tests

default_env = {
	k: v
	for k, v in os.environ.items()
	if genvm_tool.tests.util.environ.DEFAULT_FILTER(k, v)
}

default_env['AFL_FUZZER_LOOPCOUNT'] = '20'  # without it no coverage will be written!
default_env['AFL_NO_CFG_FUZZING'] = '1'
default_env['AFL_BENCH_UNTIL_CRASH'] = '1'
default_env.pop('VIRTUAL_ENV', None)

local_ctx = genvm_tool.tests.stage.configuration.current_context()

# The Python unit-test interpreter is provided by a standalone, pinned nix flake
# (executors/v0.3.x/support/nix/py-test) that replaced genlayer-py-std's poetry
# env. It is realised lazily at run time (not collection time, so `test show`
# stays cheap); pytest / py-afl-fuzz are exec'd from its bin/.
py_test_flake_dir = local_ctx.shared.root_dir.joinpath(
	'executors', 'v0.3.x', 'support', 'nix', 'py-test'
)
py_test_env_link = local_ctx.shared.root_dir.joinpath(
	'build', 'ya-build', 'py-test-env'
)


def nix_env_command(argv: collections.abc.Sequence[str | Path]) -> list[str]:
	"""Wrap ``argv`` so it runs against the py-test flake's Python env.

	Builds (or reuses, via the gc-root out-link) the flake's env under
	``build/ya-build/py-test-env``, then execs ``<env>/bin/<argv[0]>`` with the
	remaining args. ``argv[0]`` is a bin name (``pytest``, ``py-afl-fuzz``); the
	rest pass through verbatim.
	"""
	script = (
		'set -eu\n'
		f'mkdir -p {shlex.quote(str(py_test_env_link.parent))}\n'
		'env_dir="$(nix build --print-out-paths'
		f' --out-link {shlex.quote(str(py_test_env_link))}'
		f' {shlex.quote("path:" + str(py_test_flake_dir))})"\n'
		'exec "$env_dir/bin/$0" "$@"'
	)
	return ['bash', '-c', script, *map(str, argv)]


def pytest_env(project_root_dir: Path) -> dict:
	"""``default_env`` plus PYTHONPATH for the in-tree packages under test.

	The nix env ships only the test deps; ``genlayer`` (src/) and
	``genlayer_embeddings`` / ``onnx`` (src-emb/) are imported via PYTHONPATH,
	mirroring the editable install poetry used to provide.
	"""
	env = dict(default_env)
	pythonpath = [
		str(project_root_dir.joinpath('src')),
		str(project_root_dir.joinpath('src-emb')),
	]
	existing = env.get('PYTHONPATH')
	if existing:
		pythonpath.append(existing)
	env['PYTHONPATH'] = os.pathsep.join(pythonpath)
	return env


def pytest(
	ctx: genvm_tool.tests.stage.collection.Context,
	desc: genvm_tool.tests.test.Description,
	*,
	project_root_dir: Path,
):
	desc = desc.with_tags(['python', 'unit'])._replace(console_pool=True)
	case = genvm_tool.tests.test.SimpleCommandCase(
		description=desc,
		command=nix_env_command(['pytest', '--color=yes']),
		cwd=project_root_dir,
		env=pytest_env(project_root_dir),
		mode=genvm_tool.tests.exec.command.RunMode.INTERACTIVE,
	)

	ctx.add_case(case)


def py_fuzz(
	ctx: genvm_tool.tests.stage.collection.Context,
	desc: genvm_tool.tests.test.Description,
	*,
	project_root_dir: Path,
	name: str,
):
	desc = desc.with_tags(['python', 'fuzz'])._replace(
		console_pool=True,
	)
	inputs_dir = project_root_dir.joinpath('fuzz', 'inputs', name)
	outputs_dir = project_root_dir.joinpath('fuzz', 'outputs', name)
	src_file = project_root_dir.joinpath('fuzz', 'src', f'{name}.py')

	# NB: the py-test flake omits python-afl (not in nixpkgs), so `py-afl-fuzz`
	# is absent from its bin/. The fuzz collector is disabled; re-add python-afl
	# to the flake before enabling this.
	case = genvm_tool.tests.test.SimpleCommandCase(
		description=desc,
		command=nix_env_command(
			[
				'py-afl-fuzz',
				'-i',
				inputs_dir,
				'-o',
				outputs_dir,
				'-V',
				str(getattr(ctx.configuration.args, 'fuzz_timeout', 30)),
				'--',
				src_file,
			]
		),
		cwd=project_root_dir,
		env=pytest_env(project_root_dir),
		mode=genvm_tool.tests.exec.command.RunMode.INTERACTIVE,
	)

	ctx.add_case(case)
