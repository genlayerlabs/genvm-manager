import collections.abc
import os
import shlex
from pathlib import Path

import genvm_tool.tests
import genvm_tool_plugins.afl as afl

default_env = {
	k: v
	for k, v in os.environ.items()
	if genvm_tool.tests.util.environ.DEFAULT_FILTER(k, v)
}

default_env.pop('VIRTUAL_ENV', None)

local_ctx = genvm_tool.tests.stage.configuration.current_context()

# The Python unit-test interpreter is provided by a standalone, pinned nix flake
# (executors/v0.3.x/support/nix/py-test) that replaced genlayer-py-std's poetry
# env. It is realised lazily at run time (not collection time, so `test show`
# stays cheap); tools are exec'd from its bin/.
py_test_flake_dir = local_ctx.shared.root_dir.joinpath(
	'executors', 'v0.3.x', 'support', 'nix', 'py-test'
)
py_test_env_link = local_ctx.shared.root_dir.joinpath(
	'build', 'ya-build', 'py-test-env'
)


def env_flake_dir(project_root_dir: Path) -> Path:
	"""
	The flake whose env a project's tests run under.

	A project carrying its own `flake.nix` gets that one: its tests need its own
	dependencies, and the shared env holds only what genlayer-py-std's need.
	"""
	if project_root_dir.joinpath('flake.nix').is_file():
		return project_root_dir
	return py_test_flake_dir


def nix_env_command(
	argv: collections.abc.Sequence[str | Path],
	flake_dir: Path | None = None,
) -> list[str]:
	"""
	Wrap ``argv`` so it runs against a py-test flake's Python env.

	Builds (or reuses, via the gc-root out-link) the flake's env under
	``build/ya-build/py-test-env``, then execs ``<env>/bin/<argv[0]>`` with the
	remaining args. ``argv[0]`` is a bin name (for example ``pytest``); the
	rest pass through verbatim.
	"""
	flake_dir = flake_dir or py_test_flake_dir
	# One out-link per flake, or the envs would evict each other's gc-root and
	# the next run would rebuild
	out_link = py_test_env_link.with_name(
		f'{py_test_env_link.name}-{flake_dir.name}'
		if flake_dir != py_test_flake_dir
		else py_test_env_link.name
	)
	script = (
		'set -eu\n'
		f'mkdir -p {shlex.quote(str(out_link.parent))}\n'
		'env_dir="$(nix build --print-out-paths'
		f' --out-link {shlex.quote(str(out_link))}'
		f' {shlex.quote("path:" + str(flake_dir))})"\n'
		'export PATH="$env_dir/bin:$PATH"\n'
		'exec "$env_dir/bin/$0" "$@"'
	)
	return ['bash', '-c', script, *map(str, argv)]


def pytest_env(project_root_dir: Path) -> dict:
	"""
	``default_env`` plus PYTHONPATH for the in-tree packages under test.

	The nix env ships only the dependencies; the package under test is imported
	from the working tree, mirroring the editable install poetry used to
	provide. That is ``src/`` + ``src-emb/`` for genlayer-py-std, and the
	project root itself for a flat layout such as `genvm_tool`.
	"""
	env = dict(default_env)
	pythonpath = [
		str(project_root_dir),
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
		command=nix_env_command(['pytest', '--color=yes'], env_flake_dir(project_root_dir)),
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
	desc = desc.with_tags(['python', 'fuzz', 'needs-fuzz'])._replace(
		**afl.pool_usage(ctx),
	)
	inputs_dir = project_root_dir.joinpath('fuzz', 'inputs', name)
	out_dir = afl.output_dir(local_ctx.shared, project_root_dir, name)
	src_file = project_root_dir.joinpath('fuzz', 'src', f'{name}.py')

	# A target imports its project's own packages, so it runs under the same env
	# as that project's unit tests; python-afl and AFL++ are in both
	flake_dir = env_flake_dir(project_root_dir)

	def env_command(argv: collections.abc.Sequence[str | Path]) -> list[str]:
		return nix_env_command(argv, flake_dir)

	env = afl.environment(pytest_env(project_root_dir))
	# What `py-afl-fuzz` exports before handing over to `afl-fuzz`. `AFL_SKIP_CHECKS`
	# is left out: it is the pre-1.20b spelling, which the pinned AFL++ only warns
	# about. The rest of the wrapper is the PATH lookup `nix_env_command` does
	env.update(
		{
			'AFL_DUMB_FORKSRV': '1',
			'AFL_SKIP_BIN_CHECK': '1',
			'PYTHON_AFL_PERSISTENT': '1',
			'PYTHON_AFL_SIGNAL': 'SIGUSR1',
		}
	)

	steps: list[genvm_tool.tests.exec.step.Step] = list(
		afl.fleet_steps(
			ctx,
			cwd=project_root_dir,
			env=env,
			inputs_dir=inputs_dir,
			out_dir=out_dir,
			launcher=['afl-fuzz'],
			target=['--', src_file],
			wrap=env_command,
			# Realising the env once: every fleet member would otherwise pay a
			# `nix build` before `exec`, staggering the instances' `-V` windows
			prepare_command=env_command(['python3', '-c', '']),
			status_command=env_command(['afl-whatsup', '-s', out_dir]),
			# The target is a forkserver, so a saved input replays through AFL
			# rather than by piping it into the script
			replay=shlex.join(
				str(arg)
				for arg in env_command(['afl-showmap', '-o', '/dev/null', '--', src_file])
			)
			+ ' <',
		)
	)

	steps.extend(
		afl.corpus_update_steps(
			ctx,
			inputs_dir=inputs_dir,
			out_dir=out_dir,
			cmin_command=lambda queue_dir, opt_dir: env_command(
				[
					'afl-cmin',
					'-T',
					'all',
					'-o',
					opt_dir,
					'-i',
					queue_dir,
					'--',
					src_file,
				]
			),
			# `afl-cmin` measures one input per run through `afl-showmap`, so it
			# gets the plain forkserver `py-afl-cmin` gives it; kept, the
			# persistent loop shifts which inputs survive minimization
			unset_env=('AFL_DUMB_FORKSRV', 'PYTHON_AFL_PERSISTENT'),
		)
	)

	case = genvm_tool.tests.test.StepsCase(description=desc, steps=steps)

	ctx.add_case(case)
