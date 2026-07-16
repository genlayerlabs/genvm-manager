"""Manager-root genvm-tool project config.

Loaded once by genvm-tool (``common.load_project``) before any subcommand runs;
subcommands ask it for what they need:

- ``tests(ctx)`` — the umbrella test suite (was the top-level ``.ya-test.py``):
	imports the plugins it needs and registers collectors + CLI args on the
	runner's configuration ``Context``. This is the plugins' "initial run" hook;
	plugins themselves are plain importable modules (no test-runner-specific
	registry). Per-suite test definitions live next to the tests in
	``tests/system/<name>/test.py`` and are pulled in via ``ctx.collect_dir``.
	Heavy imports stay inside this function — they need the plugin search path
	(``extra_python_paths`` in ``.genvm-monorepo-root``), which the test command
	applies before calling us.

General runner config (``artifacts_dir`` / ``extra_python_paths``) lives in
``.genvm-monorepo-root``. Each executor carries its own ``.genvm-tool.py``.
Commit hooks live in each repo's flake (git-hooks.nix), not here.
"""


def tests(ctx):
	"""Umbrella test suite (was the top-level .ya-test.py).

	``ctx`` is the runner's configuration ``Context``. Collectors close over the
	imports below (resolved lazily when collection runs).
	"""
	import json
	import sys
	from pathlib import Path

	import genvm_tool.cmd_configure
	import genvm_tool.tests

	_info_path = ctx.shared.root_dir / 'build' / 'info.json'
	if not _info_path.exists():
		ctx.shared.logger.warning('build/info.json not found, generating default')
		_build_dir = ctx.shared.root_dir / 'build'
		_build_dir.mkdir(parents=True, exist_ok=True)
		_info_path.write_text(
			json.dumps(
				{
					'coverage_dir': str(_build_dir / 'cov'),
					'build_dir': str(_build_dir),
					'rust_target_dir': str(_build_dir / 'ya-build' / 'rust-target'),
					# executor_versions / primary_executor_version: resolved from
					# source (.genvm-monorepo-root + committed manifests), so a
					# build-less test run still knows which out/executor/<real> dir
					# to reroute to. configure emits the same keys.
					**genvm_tool.cmd_configure.build_independent_info(ctx.shared.root_dir),
				},
				indent=2,
			)
			+ '\n'
		)

	ctx.run_parser.add_argument(
		'--fuzz-timeout',
		type=int,
		default=30,
		help='Timeout for each fuzzing run in seconds',
	)

	ctx.run_parser.add_argument(
		'--fuzz-update-corpus',
		default=False,
		action='store_true',
		help='Whether to update the fuzzing corpus',
	)

	import genvm_tool_plugins

	ctx.shared.logger.trace(
		'import path', path=sys.path, plugins_path=genvm_tool_plugins.__path__
	)
	from genvm_tool_plugins import (
		cargo,
		genvm,
		integration,
		pytest,
	)

	def collect_rust(ctx: genvm_tool.tests.stage.collection.Context):
		for t in filter(lambda x: x.name == 'Cargo.toml', ctx.shared.git_files):
			ctx.shared.logger.debug('discovered Cargo.toml', path=t)
			rust_root_dir = t.parent

			cargo.cargo_test(
				ctx,
				rust_root_dir=rust_root_dir,
			)

			fuzz_files = list(rust_root_dir.glob('fuzz/*.rs'))
			fuzz_files.sort()
			for fuzz_file in fuzz_files:
				ctx.shared.logger.debug('discovered fuzz target', path=fuzz_file)

				name = fuzz_file.relative_to(ctx.shared.root_dir)
				name = f'{name.parent}/{name.stem}'
				cargo.cargo_fuzz(
					ctx,
					genvm_tool.tests.test.Description(
						name,
						console_pool=True,
					),
					rust_root_dir=rust_root_dir,
					name=fuzz_file.stem,
				)

	def collect_pytest(ctx: genvm_tool.tests.stage.collection.Context):
		p = ctx.shared.root_dir.joinpath(
			'executors', 'v0.3.x', 'runners', 'genlayer-py-std'
		)
		pytest.pytest(
			ctx,
			genvm_tool.tests.test.Description(
				str(p.relative_to(ctx.shared.root_dir)),
				console_pool=True,
				tags=frozenset({'unit'}),
			),
			project_root_dir=p,
		)

		fuzz_files = list(p.glob('fuzz/src/*.py'))
		fuzz_files.sort()
		for fuzz_file in fuzz_files:
			name = fuzz_file.relative_to(ctx.shared.root_dir)
			name = f'{name.parent}/{name.stem}'
			continue  # for now let's disable it
			pytest.py_fuzz(
				ctx,
				genvm_tool.tests.test.Description(
					name,
				),
				project_root_dir=p,
				name=fuzz_file.stem,
			)

	ctx.add_collector(collect_rust)
	ctx.add_collector(collect_pytest)

	ctx.run_parser.add_argument(
		'--genvm-reroute-to',
		type=str,
		default='',
		help='Reroute GenVM runs to the given executor dir; defaults to the '
		'primary built version from build/info.json',
	)

	ctx.run_parser.add_argument(
		'--no-manager',
		default=False,
		action='store_true',
		help='Do not start manager, modules, or webdriver services (assumes manager is already running)',
	)

	ctx.run_parser.add_argument(
		'--no-webdriver',
		default=False,
		action='store_true',
		help='Do not start the webdriver service (assumes an existing webdriver is reachable on the standard port)',
	)

	def collect_integration(ctx: genvm_tool.tests.stage.collection.Context):
		# Load build info to find binary paths
		build_info = json.loads(
			ctx.shared.root_dir.joinpath('build', 'info.json').read_text()
		)
		build_dir = Path(build_info['build_dir'])

		tests_output_root = ctx.shared.artifacts_dir.joinpath('integration')
		tests_output_root.mkdir(parents=True, exist_ok=True)

		# Reroute runs to the locally-built executor dir; the manager honors this
		# per-request only under debug_mode >= safe (tests run with unsafe).
		reroute_to = (
			getattr(ctx.configuration.args, 'genvm_reroute_to', '')
			or build_info.get('primary_executor_version')
			or ctx.shared.config['active-versions'][0]
		)
		no_manager = getattr(ctx.configuration.args, 'no_manager', False)
		no_webdriver = getattr(ctx.configuration.args, 'no_webdriver', False)
		ci = ctx.shared.ci

		manager_port = genvm.get_manager_port(ctx.configuration)

		if no_manager:
			manager_impl = genvm.ExternalManagerService(port=manager_port)
			webdriver_impl = genvm.NoOpService()
			modules_impl = genvm.NoOpService()
		else:
			manager_impl = genvm.ManagerService(
				bin_path=build_dir.joinpath('out', 'bin', 'genvm-modules'),
				log_path=tests_output_root.joinpath('manager.log'),
				env=ctx.configuration,
				ci=ci,
			)
			# Create webdriver service
			if no_webdriver:
				webdriver_impl = genvm.NoOpService()
			else:
				webdriver_impl = genvm_tool.tests.exec.service.FunctionService(
					lambda: genvm.start_webdriver_service(ctx.configuration, ci=ci)
				)
			# This starts Llm and Web modules on the manager
			modules_impl = genvm.ModulesService(
				manager_uri=f'http://localhost:{manager_port}',
			)

		manager_service = ctx.new_service(
			name='manager',
			manager=manager_impl,
		)
		manager_service.meta = {'port': manager_port, 'reroute_to': reroute_to}

		webdriver_service = ctx.new_service(
			name='webdriver',
			manager=webdriver_impl,
		)

		modules_service = ctx.new_service(
			name='modules',
			manager=modules_impl,
			depends_on=[] if no_manager else [manager_service, webdriver_service],
		)

		# Collect integration tests
		integration.integration_test(
			ctx,
			manager_service=manager_service,
			modules_service=modules_service,
			webdriver_service=webdriver_service,
			ci=ci,
		)

		ctx.collect_dir('tests/system/permits', manager_service=manager_service)

	ctx.add_collector(collect_integration)

	def collect_parse_version(ctx: genvm_tool.tests.stage.collection.Context):
		ctx.collect_dir('tests/system/parse_version')

	ctx.add_collector(collect_parse_version)
