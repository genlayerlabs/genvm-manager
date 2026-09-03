import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import genvm_tool.tests
import genvm_tool_plugins.afl as afl
import genvm_tool_plugins.source_tags as source_tags
import tomllib

local_ctx = genvm_tool.tests.stage.configuration.current_context()

build_info = json.loads(
	local_ctx.shared.root_dir.joinpath('build', 'info.json').read_text()
)

BUILD_DIR = Path(build_info['build_dir'])
TARGET_DIR = Path(build_info['rust_target_dir'])
COVERAGE_DIR = Path(build_info['coverage_dir'])

if 'rust_target_dirs' not in build_info or 'rust_target' not in build_info:
	raise RuntimeError(
		'build/info.json predates per-line cargo target dirs, re-run `genvm-tool configure`'
	)

# Line checkout -> its own cargo target dir; see `rust_target_dirs_info`. Falling
# back to the shared dir on a stale info.json would resurrect the cross-line
# artifact clobbering this exists to prevent, hence the hard error above.
_LINE_TARGET_DIRS = {
	Path(mount): Path(target_dir)
	for mount, target_dir in build_info['rust_target_dirs'].items()
}

# Passed to every cargo invocation, matching the ninja build: without it cargo
# builds a separate host unit graph and shares nothing with what ninja compiled.
RUST_TARGET = build_info['rust_target']

# Add --coverage flag to run parser
local_ctx.run_parser.add_argument(
	'--coverage',
	action='store_true',
	default=False,
	help='Enable coverage collection for Rust tests',
)

# Track profile objects for coverage (populated during collection)
_profile_objects: list[Path] = []


def _is_coverage_enabled() -> bool:
	"""Check if coverage is enabled (after args are parsed)."""
	return '--coverage' in sys.argv


def _get_default_env() -> dict[str, str]:
	"""Get default environment for cargo commands."""
	env = {
		k: v
		for k, v in os.environ.items()
		if genvm_tool.tests.util.environ.DEFAULT_FILTER(k, v)
	}

	cargo_ld_library_path = os.environ.get('CARGO_LD_LIBRARY_PATH', None)
	base_ld_library_path = env.get('LD_LIBRARY_PATH', None)
	new_ld_library_path = ':'.join(
		filter(None, [cargo_ld_library_path, base_ld_library_path])
	)
	env['LD_LIBRARY_PATH'] = new_ld_library_path

	if _is_coverage_enabled():
		# Enable coverage instrumentation
		env['RUSTFLAGS'] = '-C instrument-coverage'
		env['LLVM_PROFILE_FILE'] = str(COVERAGE_DIR / 'cov-%p-%16m.profraw')
	else:
		env['LLVM_PROFILE_FILE'] = '/dev/null'

	return env


def _find_llvm_tool(tool_name: str) -> str:
	"""Find LLVM tool, preferring the one from rustc's toolchain."""
	# Try to find from rustc's toolchain first
	try:
		result = subprocess.run(
			['rustc', '--print', 'target-libdir'],
			capture_output=True,
			text=True,
			check=True,
		)
		llvm_bin = Path(result.stdout.strip()).parent / 'bin'
		tool_path = llvm_bin / tool_name
		if tool_path.exists():
			return str(tool_path)
	except (subprocess.CalledProcessError, FileNotFoundError):
		pass

	# Fall back to system PATH
	system_tool = shutil.which(tool_name)
	if system_tool:
		return system_tool

	raise RuntimeError(f'{tool_name} not found')


def _coverage_post_run(
	shared: genvm_tool.tests.SharedContext,
	execution_env: genvm_tool.tests.stage.execution.Env,
) -> None:
	"""Post-run step for coverage collection."""
	shared.logger.info('Collecting coverage data', coverage_dir=str(COVERAGE_DIR))

	# Find all .profraw files
	profraw_files = list(COVERAGE_DIR.glob('*.profraw'))
	if not profraw_files:
		shared.logger.warning('No .profraw files found', coverage_dir=str(COVERAGE_DIR))
		return

	shared.logger.info('Found profraw files', count=len(profraw_files))

	# Write file list
	files_list_path = COVERAGE_DIR / 'files-list'
	files_list_path.write_text('\n'.join(str(f) for f in profraw_files))

	# Find LLVM tools
	try:
		llvm_profdata = _find_llvm_tool('llvm-profdata')
		llvm_cov = _find_llvm_tool('llvm-cov')
	except RuntimeError as e:
		shared.logger.error('LLVM tools not found', error=str(e))
		return

	# Merge profraw files
	merged_profdata = COVERAGE_DIR / 'merged.profdata'
	shared.logger.info('Merging profile data')

	merge_cmd = [
		llvm_profdata,
		'merge',
		'-sparse',
		'-f',
		str(files_list_path),
		'-o',
		str(merged_profdata),
	]

	result = subprocess.run(merge_cmd, capture_output=True, text=True)
	if result.returncode != 0:
		shared.logger.error(
			'Failed to merge profile data',
			returncode=result.returncode,
			stderr=result.stderr,
		)
		return

	# Collect existing profile objects, expanding directories
	existing_objects: list[Path] = []
	for obj in _profile_objects:
		if obj.is_dir():
			# For directories (like deps/), find executable files
			for child in obj.iterdir():
				if child.is_file() and not child.suffix and child.stat().st_mode & 0o111:
					existing_objects.append(child)
		elif obj.exists():
			existing_objects.append(obj)

	if not existing_objects:
		shared.logger.warning('No profile objects found')
		return

	shared.logger.info('Found profile objects', count=len(existing_objects))

	# Generate coverage report
	report_path = COVERAGE_DIR / 'report.txt'
	shared.logger.info('Generating coverage report')

	cov_cmd = [
		llvm_cov,
		'report',
		'-format=text',
		f'-instr-profile={merged_profdata}',
		'--ignore-filename-regex=(^|/)(\\.cargo|\\.rustup|third-party)/|cranelift|target-lexicon',
	]

	for obj in existing_objects:
		cov_cmd.extend(['--object', str(obj)])

	result = subprocess.run(cov_cmd, capture_output=True, text=True)
	if result.returncode != 0:
		shared.logger.error(
			'Failed to generate coverage report',
			returncode=result.returncode,
			stderr=result.stderr,
		)
		return

	# Write and display report
	report_path.write_text(result.stdout)
	shared.printer.put('coverage report', path=str(report_path))

	# Print the report to stdout
	print(result.stdout)


# Register coverage post-run step if coverage is enabled
if _is_coverage_enabled():
	local_ctx.add_reporter(_coverage_post_run)
	# Add genvm binaries to profile objects
	_profile_objects.append(BUILD_DIR / 'out' / 'bin' / 'genvm')
	_profile_objects.append(BUILD_DIR / 'out' / 'bin' / 'genvm-modules')


def _target_dir(rust_root_dir: Path) -> Path:
	"""Target dir for a crate: its line's, or the shared one if it is in no line."""
	rel = rust_root_dir.relative_to(local_ctx.shared.root_dir)
	for mount, target_dir in _LINE_TARGET_DIRS.items():
		if rel.is_relative_to(mount):
			return target_dir
	return TARGET_DIR


def _artifact_dir(target_dir: Path) -> Path:
	"""Where `--target` puts built artifacts, as opposed to host build scripts."""
	return target_dir / RUST_TARGET / 'debug'


def _load_cargo_config(
	ctx: genvm_tool.tests.stage.collection.Context,
	rust_root_dir: Path,
	flags_key: str = 'cargo_test_flags',
) -> tuple[dict, dict[str, str], list[str], dict[str, list[str]]]:
	"""Load .ya-test-config.json and return config, env, flags and case tags."""
	test_env = _get_default_env()
	extra_flags: list[str] = []
	case_tags: dict[str, list[str]] = {}

	extra_config = rust_root_dir.joinpath('.ya-test-config.json')
	extra_conf: dict = {}
	if extra_config.exists():
		extra_conf = json.loads(extra_config.read_text())
		extra_flags.extend(extra_conf.get(flags_key, []))
		for name in extra_conf.get('keep_env', []):
			if name in os.environ:
				test_env[name] = os.environ[name]
		raw_case_tags = extra_conf.get('tags', {})
		if not isinstance(raw_case_tags, dict):
			raise ValueError(f'{extra_config}: tags must be an object')
		for case_name, raw_tags in raw_case_tags.items():
			if case_name != 'lib' and not case_name.startswith('bin/'):
				raise ValueError(f'{extra_config}: invalid tag target {case_name!r}')
			if not isinstance(raw_tags, list) or not all(
				isinstance(tag, str) for tag in raw_tags
			):
				raise ValueError(
					f'{extra_config}: tags for {case_name!r} must be a list of strings'
				)
			case_tags[case_name] = source_tags.validate_declared(ctx, extra_config, raw_tags)

	return extra_conf, test_env, extra_flags, case_tags


def _add_cargo_case(
	ctx: genvm_tool.tests.stage.collection.Context,
	*,
	name: str,
	command: list,
	rust_root_dir: Path,
	test_env: dict[str, str],
	tags: list[str],
):
	desc = (
		genvm_tool.tests.test.Description(name)
		.with_tags(tags)
		._replace(
			console_pool=True,
		)
	)
	case = genvm_tool.tests.test.SimpleCommandCase(
		description=desc,
		command=command,
		cwd=rust_root_dir,
		env=test_env,
		mode=genvm_tool.tests.exec.command.RunMode.INTERACTIVE,
	)
	ctx.add_case(case)


def _is_skipped(skip_conf, key: str, name: str | None = None) -> bool:
	"""
	Check if a test category or specific name is skipped.

	skip_conf[key] can be:
		- true: skip all
		- list of strings: skip only those names
	"""
	val = skip_conf.get(key)
	if val is True:
		return True
	if isinstance(val, list) and name is not None:
		return name in val
	return False


def cargo_test(
	ctx: genvm_tool.tests.stage.collection.Context,
	*,
	rust_root_dir: Path,
):
	extra_conf, test_env, extra_flags, case_tags = _load_cargo_config(ctx, rust_root_dir)
	skip_conf = extra_conf.get('skip', {})

	rel_dir = rust_root_dir.relative_to(local_ctx.shared.root_dir)

	cargo_toml = tomllib.loads(rust_root_dir.joinpath('Cargo.toml').read_text())
	has_lib = 'lib' in cargo_toml or rust_root_dir.joinpath('src', 'lib.rs').exists()
	bins: list[dict] = list(cargo_toml.get('bin', []))
	if rust_root_dir.joinpath('src', 'main.rs').exists():
		pkg_name = cargo_toml.get('package', {}).get('name', rust_root_dir.name)
		if not any(binary.get('name') == pkg_name for binary in bins):
			bins.append({'name': pkg_name, 'path': 'src/main.rs'})
	known_tag_targets = {'lib'} if has_lib else set()
	known_tag_targets.update(f'bin/{binary["name"]}' for binary in bins)
	unknown_tag_targets = set(case_tags) - known_tag_targets
	if unknown_tag_targets:
		extra_config = rust_root_dir / '.ya-test-config.json'
		raise ValueError(
			f'{extra_config}: tags declared for unknown Cargo cases: '
			f'{", ".join(sorted(unknown_tag_targets))}'
		)

	target_dir = _target_dir(rust_root_dir)

	# Track deps directory for coverage
	if _is_coverage_enabled():
		deps_dir = _artifact_dir(target_dir) / 'deps'
		if deps_dir not in _profile_objects:
			_profile_objects.append(deps_dir)

	base_cmd = [
		'cargo',
		'test',
		'--message-format=short',
		'--color=always',
		'--target',
		RUST_TARGET,
		'--target-dir',
		str(target_dir),
	] + extra_flags

	# 1. Examples - verify compilation
	if not _is_skipped(skip_conf, 'examples'):
		for ex in cargo_toml.get('example', []):
			ex_name = ex['name']
			if _is_skipped(skip_conf, 'examples', ex_name):
				continue
			ex_path = ex.get('path', f'examples/{ex_name}.rs')
			if ex_path.startswith('fuzz/'):
				continue
			_add_cargo_case(
				ctx,
				name=str(rel_dir / ex_path),
				command=[
					'cargo',
					'check',
					'--color=always',
					'--target',
					RUST_TARGET,
					'--target-dir',
					str(target_dir),
					'--example',
					ex_name,
				]
				+ extra_flags,
				rust_root_dir=rust_root_dir,
				test_env=test_env,
				tags=['rust', 'example']
				+ source_tags.from_source(ctx, rust_root_dir / ex_path),
			)

	# 2. Test files in tests/
	test_dir = rust_root_dir / 'tests'
	if test_dir.exists() and not _is_skipped(skip_conf, 'tests'):
		for test_file in sorted(test_dir.glob('*.rs')):
			test_name = test_file.stem
			if _is_skipped(skip_conf, 'tests', test_name):
				continue
			_add_cargo_case(
				ctx,
				name=str(rel_dir / 'tests' / test_file.name),
				command=base_cmd + ['--test', test_name],
				rust_root_dir=rust_root_dir,
				test_env=test_env,
				tags=['rust', 'unit'] + source_tags.from_source(ctx, test_file),
			)

	# 3. --lib test
	if has_lib and not _is_skipped(skip_conf, 'lib'):
		_add_cargo_case(
			ctx,
			name=str(rel_dir / 'lib'),
			command=base_cmd + ['--lib'],
			rust_root_dir=rust_root_dir,
			test_env=test_env,
			tags=['rust', 'unit'] + case_tags.get('lib', []),
		)

	# 4. --bin tests for every binary
	if not _is_skipped(skip_conf, 'bins'):
		for bin_entry in bins:
			bin_name = bin_entry['name']
			if _is_skipped(skip_conf, 'bins', bin_name):
				continue
			_add_cargo_case(
				ctx,
				name=str(rel_dir / 'bin' / bin_name),
				command=base_cmd + ['--bin', bin_name],
				rust_root_dir=rust_root_dir,
				test_env=test_env,
				tags=['rust', 'unit'] + case_tags.get(f'bin/{bin_name}', []),
			)


def cargo_fuzz(
	ctx: genvm_tool.tests.stage.collection.Context,
	desc: genvm_tool.tests.test.Description,
	*,
	rust_root_dir: Path,
	name: str,
):
	desc = desc.with_tags(
		['rust', 'fuzz', 'needs-fuzz']
		+ source_tags.from_source(ctx, rust_root_dir / 'fuzz' / f'{name}.rs')
	)._replace(
		**afl.pool_usage(ctx),
	)

	_extra_conf, test_env, extra_flags, _case_tags = _load_cargo_config(
		ctx, rust_root_dir, 'cargo_afl_build_flags'
	)

	target_dir = _target_dir(rust_root_dir)

	# Fake entropy, so that a saved crash replays: `std`'s randomness seeds
	# tokio's scheduler and every `HashMap`. `test_env` is a fresh copy, keeping
	# this out of `cargo test`; the shim reaches the target through AFL, which
	# turns `AFL_PRELOAD` into an `LD_PRELOAD` for the child only.
	preload_manifest = (
		local_ctx.shared.root_dir / 'crates' / 'fuzzing' / 'preload' / 'Cargo.toml'
	)
	test_env['AFL_PRELOAD'] = str(_artifact_dir(target_dir) / 'libgenvm_fuzz_preload.so')
	# A target with a mutator crate next to it takes its input as a serialized
	# value, and the crate mutates that value structurally instead of editing
	# its bytes.
	#
	# AFL's own mutations are deliberately left on. `AFL_CUSTOM_MUTATOR_ONLY`
	# looks right — a havoc'd buffer usually fails to decode, and the target
	# rejects it — but it measures worse: on `genvm-common-encode` over 15s,
	# 20.55% coverage with `_ONLY` against 27.48% without, and 24.07% for the
	# `arbitrary` generator this replaced. A rejected input costs one cheap
	# execution, while the ones that do decode reach values the structural
	# mutator does not propose. Set `GENVM_FUZZ_MUTATOR_ONLY=1` to compare.
	mutator_manifest = rust_root_dir / 'fuzz' / 'mutators' / name / 'Cargo.toml'
	if mutator_manifest.is_file():
		library = f'libfuzz_mutator_{name.replace("-", "_")}.so'
		test_env['AFL_CUSTOM_MUTATOR_LIBRARY'] = str(_artifact_dir(target_dir) / library)
		if os.environ.get('GENVM_FUZZ_MUTATOR_ONLY') == '1':
			test_env['AFL_CUSTOM_MUTATOR_ONLY'] = '1'
	else:
		mutator_manifest = None

	out_dir = afl.output_dir(local_ctx.shared, rust_root_dir, name)

	fuzz_binary = _artifact_dir(target_dir) / 'examples' / f'fuzz-{name}'

	# Track fuzz binary for coverage
	if _is_coverage_enabled():
		if fuzz_binary not in _profile_objects:
			_profile_objects.append(fuzz_binary)

	inputs_dir = rust_root_dir / 'fuzz' / f'inputs-{name}'
	fuzz_env = afl.environment(test_env)
	# Drops one "attempting dry run" line per corpus entry; seeds are calibrated
	# lazily instead, so broken ones surface when they are first fuzzed. Rust
	# only: python-afl's forkserver does not survive skipping the dry run
	fuzz_env['AFL_NO_STARTUP_CALIBRATION'] = '1'
	# `fuzz_env` is the whole environment of this case, secondaries included, so
	# what the builds below need lives in it too
	fuzz_env['CARGO'] = 'cargo'

	steps: list[genvm_tool.tests.exec.step.Step] = []
	steps.extend(
		[
			genvm_tool.tests.exec.step.SetCwd(path=rust_root_dir),
		]
		# The builds below need the AFL env too: without `AFL_NO_CFG_FUZZING` the
		# example is built with `--cfg fuzzing`, which pulls `libfuzzer-sys` in
		# through a dependency and collides with AFL's own coverage runtime
		+ [genvm_tool.tests.exec.step.SetEnv(key=k, value=v) for k, v in fuzz_env.items()]
		+ [
			genvm_tool.tests.exec.step.Run(
				args=[
					'cargo',
					'build',
					'--manifest-path',
					str(preload_manifest),
					'--target',
					RUST_TARGET,
					'--target-dir',
					target_dir,
					'--color=always',
				],
				mode=genvm_tool.tests.exec.command.RunMode.INTERACTIVE,
			),
			genvm_tool.tests.test.CommandToResultStep(),
			genvm_tool.tests.test.ResultStopIfErrorStep(),
		]
		+ (
			[]
			if mutator_manifest is None
			else [
				genvm_tool.tests.exec.step.Run(
					args=[
						'cargo',
						'build',
						'--manifest-path',
						str(mutator_manifest),
						'--target',
						RUST_TARGET,
						'--target-dir',
						target_dir,
						'--color=always',
					],
					mode=genvm_tool.tests.exec.command.RunMode.INTERACTIVE,
				),
				genvm_tool.tests.test.CommandToResultStep(),
				genvm_tool.tests.test.ResultStopIfErrorStep(),
			]
		)
		+ [
			genvm_tool.tests.exec.step.Run(
				args=[
					'cargo-afl',
					'afl',
					'build',
					'--target',
					RUST_TARGET,
					'--target-dir',
					target_dir,
					'--example',
					f'fuzz-{name}',
					'--color=always',
				]
				+ extra_flags,
				mode=genvm_tool.tests.exec.command.RunMode.INTERACTIVE,
			),
			genvm_tool.tests.test.CommandToResultStep(),
			genvm_tool.tests.test.ResultStopIfErrorStep(),
		]
	)
	steps.extend(
		afl.fleet_steps(
			ctx,
			cwd=rust_root_dir,
			env=fuzz_env,
			inputs_dir=inputs_dir,
			out_dir=out_dir,
			launcher=['cargo-afl', 'afl', 'fuzz'],
			target=[fuzz_binary],
			status_command=['cargo-afl', 'afl', 'whatsup', '-s', out_dir],
			# A persistent-mode target expects a forkserver, so replaying an input
			# means going through AFL rather than piping it into the binary. The
			# preload has to come along, or the replay reseeds itself
			replay=(
				f'AFL_PRELOAD={fuzz_env["AFL_PRELOAD"]} '
				f'cargo-afl afl showmap -o /dev/null -- {fuzz_binary} <'
			),
		)
	)

	steps.extend(
		afl.corpus_update_steps(
			ctx,
			inputs_dir=inputs_dir,
			out_dir=out_dir,
			cmin_command=lambda queue_dir, opt_dir: [
				'cargo-afl',
				'afl',
				'cmin',
				'-T',
				'all',
				'-o',
				opt_dir,
				'-i',
				queue_dir,
				'--',
				fuzz_binary,
			],
		)
	)

	case = genvm_tool.tests.test.StepsCase(
		description=desc,
		steps=steps,
	)

	ctx.add_case(case)
