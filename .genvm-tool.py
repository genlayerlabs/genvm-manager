"""Manager-root genvm-tool project config.

Loaded once by genvm-tool (``common.load_project``) before any subcommand runs;
subcommands ask it for what they need:

- ``hooks(ctx)`` — the manager's commit-hook definitions (was
	``support/nix/precommit/hooks.toml``), returned in the same shape the hook
	engine already understands.
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
``.genvm-monorepo-root``. Each executor carries its own ``.genvm-tool.py`` with
its own ``hooks``.
"""


def hooks(ctx):
	"""Manager commit-hook definitions (was support/nix/precommit/hooks.toml).

	Tools resolve from the sibling flake's buildEnv (``nix = "<flake output>"``);
	``local`` hooks run a repo-owned script and ``builtin`` hooks run logic baked
	into genvm-tool. A hook's ``args`` are its check-mode invocation; an optional
	``fix_args`` is the rewrite-in-place invocation used when ``hook run`` fixes
	(the default — CI passes ``--check``). ``files``/``exclude`` are repo-relative
	regexes.
	"""
	return [
		# --- generic checks (mirror the executor's) ------------------------
		{
			'id': 'trailing-whitespace',
			'nix': 'pre-commit-hooks',
			'entry': 'trailing-whitespace-fixer',
			'types_or': ['text'],
			'exclude': r'^\.git-third-party|/fuzz/',
		},
		{
			'id': 'end-of-file-fixer',
			'nix': 'pre-commit-hooks',
			'entry': 'end-of-file-fixer',
			'types_or': ['text'],
			'exclude': r'^\.git-third-party|/fuzz/',
		},
		{
			'id': 'check-added-large-files',
			'nix': 'pre-commit-hooks',
			'entry': 'check-added-large-files',
		},
		{
			'id': 'check-json',
			'nix': 'pre-commit-hooks',
			'entry': 'check-json',
			'types_or': ['json'],
			'exclude': r'(^\.git-third-party)|(/tsconfig\.json$)',
		},
		{
			'id': 'check-yaml',
			'nix': 'pre-commit-hooks',
			'entry': 'check-yaml',
			'types_or': ['yaml'],
		},
		{
			'id': 'check-toml',
			'nix': 'pre-commit-hooks',
			'entry': 'check-toml',
			'types_or': ['toml'],
		},
		{
			'id': 'check-merge-conflict',
			'nix': 'pre-commit-hooks',
			'entry': 'check-merge-conflict',
			'types_or': ['text'],
		},
		{
			# Matches both the hidden vendored `.git-third-party` trees and the
			# `support/tools/git-third-party` tool dir (vendored LICENSE etc.).
			'id': 'editorconfig-checker',
			'nix': 'editorconfig-checker',
			'entry': 'editorconfig-checker',
			# text-only: the engine's `text` pseudo-type filters out binaries
			# (e.g. model/onnx blobs) that editorconfig-checker should not see.
			'types_or': ['text'],
			'exclude': r'git-third-party|/fuzz/',
		},
		# --- python / lua / ts (manager-owned languages) -------------------
		{
			'id': 'ruff-format',
			'nix': 'ruff',
			'entry': 'ruff',
			'args': ['format', '--check'],
			'fix_args': ['format'],
			'types_or': ['python'],
		},
		# --- python / lua / ts (manager-owned languages) -------------------
		{
			'id': 'ruff-check',
			'nix': 'ruff',
			'entry': 'ruff',
			'args': ['check'],
			'fix_args': ['check', '--fix'],
			'types_or': ['python'],
		},
		{
			'id': 'stylua',
			'nix': 'stylua',
			'entry': 'stylua',
			'args': ['--check'],
			'fix_args': [],
			'files': r'\.lua$',
		},
		{
			'id': 'prettier',
			'nix': 'prettier',
			'entry': 'prettier',
			'args': ['--check'],
			'fix_args': ['--write'],
			'types_or': ['ts', 'tsx'],
			'exclude': r'^\.git-third-party',
		},
		{
			'id': 'nixfmt',
			'nix': 'nixfmt',
			'entry': 'nixfmt',
			'args': ['--check'],
			'fix_args': [],
			'files': r'\.nix$',
		},
		# --- github workflow/action schemas -------------------------------
		{
			'id': 'check-github-workflows',
			'nix': 'check-jsonschema',
			'entry': 'check-jsonschema',
			'args': ['--builtin-schema', 'vendor.github-workflows'],
			'files': r'^\.github/workflows/.*\.ya?ml$',
		},
		{
			'id': 'check-github-actions',
			'nix': 'check-jsonschema',
			'entry': 'check-jsonschema',
			'args': ['--builtin-schema', 'vendor.github-actions'],
			'files': r'^\.github/(actions/.+/)?action\.ya?ml$',
		},
		# --- local scripts -------------------------------------------------
		{
			'id': 'cargo-fmt',
			'nix': 'cargo',
			'builtin': 'cargo-fmt',
			'files': r'\.rs$',
			'pass_filenames': False,
		},
		{
			# Keep the manager crate's [package] version in lockstep with
			# .genvm-monorepo-root. The executor submodule is versioned
			# independently (its own repo/hooks own that); we never touch it.
			'id': 'check-cargo-versions',
			'local': True,
			'entry': 'support/ci/check-versions.py',
			'args': ['sync'],
			'pass_filenames': False,
			'files': r'^(implementation/Cargo\.toml|\.genvm-monorepo-root)$',
		},
		{
			'id': 'markdown-local-links',
			'builtin': 'md-local-links',
			'types_or': ['markdown'],
		},
	]


def tests(ctx):
	"""Umbrella test suite (was the top-level .ya-test.py).

	``ctx`` is the runner's configuration ``Context``. Collectors close over the
	imports below (resolved lazily when collection runs).
	"""
	import json
	import sys
	from pathlib import Path

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
				'runners/genlayer-py-std/test',
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
			or build_info['primary_executor_version']
		)
		no_manager = getattr(ctx.configuration.args, 'no_manager', False)
		no_webdriver = getattr(ctx.configuration.args, 'no_webdriver', False)

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
			)
			# Create webdriver service
			if no_webdriver:
				webdriver_impl = genvm.NoOpService()
			else:
				webdriver_impl = genvm_tool.tests.exec.service.FunctionService(
					lambda: genvm.start_webdriver_service(ctx.configuration)
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
		)

		ctx.collect_dir('tests/system/permits', manager_service=manager_service)

	ctx.add_collector(collect_integration)

	def collect_parse_version(ctx: genvm_tool.tests.stage.collection.Context):
		ctx.collect_dir('tests/system/parse_version')

	ctx.add_collector(collect_parse_version)
