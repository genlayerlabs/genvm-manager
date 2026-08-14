"""
`genvm-tool configure` — generate the build graph.

Writes `build/build.ninja` and `build/info.json`, from which `ninja -C build`
drives the cargo builds, `genvm-tool codegen` outputs, the runner build, and the
install step. Run it once after cloning, and again whenever the set of active
executor lines or their manifests changes. Works from any directory in the repo.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from . import common

NAME = 'configure'
HELP = 'generate build/build.ninja and build/info.json'

# Umbrella manifest listing the active top-level version lines and the primary.
MONOREPO_ROOT_FILE = '.genvm-monorepo-root'

# The importable plugins live here (mirrors `extra_python_paths`); configure adds
# it to `sys.path` so it (and each line's hook) can import `genvm_tool_plugins`.
PLUGINS_REL = Path('tests/runner')


class ExecVersion(NamedTuple):
	"""
	One active executor line resolved to its on-disk locations.

	- ``key``: the top-level version line (e.g. ``v0.3``) as listed in
		``.genvm-monorepo-root``.
	- ``exec_rel``: the checkout mount, ``executors/<key>.x`` (a git submodule).
	- ``real``: the concrete built version from that checkout's ``manifest.json``
		(e.g. ``v0.3.0-rc7``). This is the ``out/executor/<real>`` directory the
		manager resolves a run to.
	"""

	key: str
	exec_rel: str
	real: str


def _load_versions(monorepo_cfg, source_dir: Path) -> tuple[str, list[ExecVersion]]:
	"""
	Resolve every active executor line from ``.genvm-monorepo-root``.

	Each active line is mounted at ``executors/<line>.x`` and its
	``manifest.json`` pins the concrete ``executor-version`` that becomes the
	built ``out/executor/<real>`` directory. There may be several active lines.
	"""
	versions = []
	for key in monorepo_cfg['active-versions']:
		exec_rel = f'executors/{key}.x'
		manifest = json.loads((source_dir / exec_rel / 'manifest.json').read_text())
		versions.append(
			ExecVersion(key=key, exec_rel=exec_rel, real=manifest['executor-version'])
		)
	return monorepo_cfg['version'], versions


def build_independent_info(monorepo_cfg, source_dir: Path) -> dict:
	"""
	The parts of ``info.json`` derivable from source alone — no build needed.

	``executor_versions`` maps each active line (e.g. ``v0.3``) to its built
	``out/executor/<real>`` directory name; ``primary_executor_version`` is the
	primary line's. Both come from ``.genvm-monorepo-root`` and each line's
	committed ``manifest.json``, so the test harness can synthesize a usable
	``info.json`` (see the manager-root ``.genvm-tool.py``) without first running
	``configure``. ``configure`` merges this into the full, build-dependent info.
	"""
	primary_key, versions = _load_versions(monorepo_cfg, source_dir)
	primary = next((v for v in versions if v.key == primary_key), versions[0])
	return {
		'executor_versions': {v.key: v.real for v in versions},
		'primary_executor_version': primary.real,
	}


def base_info(
	monorepo_cfg, source_dir: Path, build_dir: Path, rust_target_dir: Path
) -> dict:
	"""
	Every ``info.json`` key that does not depend on having built anything.

	Two commands write ``info.json``: ``configure``, and the test harness when it
	finds none (so CI can run tests without configuring first, see the
	manager-root ``.genvm-tool.py``). ``configure`` writes this plus whatever the
	build teaches it; the harness writes exactly this. They share the function so
	the two files cannot describe the same tree differently.
	"""
	return {
		'coverage_dir': str(build_dir / 'cov'),
		'build_dir': str(build_dir),
		'rust_target_dir': str(rust_target_dir),
		# The build passes `--target` explicitly; the test harness must pass the
		# same one or it gets a second, unshared unit graph in the same dir.
		'rust_target': detect_rust_target(),
		**build_independent_info(monorepo_cfg, source_dir),
		**rust_target_dirs_info(monorepo_cfg, source_dir, rust_target_dir),
	}


def rust_target_dirs_info(
	monorepo_cfg, source_dir: Path, rust_target_dir: Path
) -> dict:
	"""
	``rust_target_dirs``: line checkout (``executors/v0.3.x``) → its cargo target dir.

	Keyed by mount so a consumer only has to ask which one contains its crate;
	crates under none of them use ``rust_target_dir`` itself. Why lines cannot
	share a dir: :func:`genvm_tool_plugins.ninja.target_dir_for_line`.
	"""
	from genvm_tool_plugins import ninja

	_, versions = _load_versions(monorepo_cfg, source_dir)
	return {
		'rust_target_dirs': {
			v.exec_rel: str(ninja.target_dir_for_line(rust_target_dir, v.key))
			for v in versions
		}
	}


def configure(parser):
	parser.add_argument(
		'--ci',
		action='store_true',
		help='CI mode: pass --locked to cargo so the build fails on a stale Cargo.lock',
	)


def detect_rust_target() -> str:
	out = subprocess.run(
		['rustc', '-vV'], capture_output=True, text=True, check=True
	).stdout
	for line in out.splitlines():
		if line.startswith('host: '):
			return line[len('host: ') :].strip()
	raise common.ToolError("failed to detect rust target from 'rustc -vV' output")


def _line_configurator(ctx: common.Context, source_dir: Path, exec_rel: str):
	"""
	Resolve a line's `configure(line)` hook from its `.genvm-tool.py`.

	Falls back to the plugin's default (committed-registry-or-nix) when the line
	carries no hook, so a line without one still configures sensibly.
	"""
	from genvm_tool_plugins import ninja

	project = common.load_project(source_dir / exec_rel)
	hook = getattr(project, 'configure', None)
	if hook is None:
		ctx.logger.debug('line has no configure hook, using default', line=exec_rel)
		return ninja.configure_line_default
	return hook


def main(ctx: common.Context, args) -> int:
	source_dir = ctx.root

	monorepo_cfg = json.loads(ctx.root.joinpath(MONOREPO_ROOT_FILE).read_text())

	# The ninja DSL + per-line helpers live in the importable plugin; put the
	# plugin search path on sys.path before importing it (and before any line's
	# `.genvm-tool.py:configure` hook, which may import it too).
	plugins_path = str(source_dir / PLUGINS_REL)
	if plugins_path not in sys.path:
		sys.path.insert(0, plugins_path)
	from genvm_tool_plugins import ninja

	build_dir = source_dir / ninja.BUILD_DIR_REL

	primary_key, versions = _load_versions(monorepo_cfg, source_dir)
	# Manager-global generated files (test fixtures, docs) have a single output,
	# so they are derived from the primary line's codegen data.
	primary = next((v for v in versions if v.key == primary_key), versions[0])
	primary_exec_root = source_dir / primary.exec_rel

	rust_target = detect_rust_target()
	rust_target_dir = build_dir / 'ya-build' / 'rust-target'
	rust_target_dir.mkdir(parents=True, exist_ok=True)

	n = ninja.Ninja(source_dir, build_dir)
	n.rust_target = rust_target
	n.rust_target_dir = rust_target_dir

	n.comment('Generated by `genvm-tool configure`, DO NOT EDIT MANUALLY')
	n.var('ninja_required_version', '1.5')
	# Default cargo target dir. Version-independent crates share it; each
	# executor line overrides it (register_cargo `target_dir=`) so two lines
	# building a `genvm` binary don't clobber the same `debug/genvm`.
	n.var('target_dir', str(rust_target_dir))

	n.rule(
		'CLEAN',
		command=[
			'ninja',
			ninja.RawStr('$FILE_ARG'),
			'-t',
			'clean',
			ninja.RawStr('$TARGETS'),
		],
		description='Cleaning all built files...',
	)
	n.rule(
		'HELP',
		command=[
			'ninja',
			ninja.RawStr('$FILE_ARG'),
			'-t',
			'targets',
			'rule',
			'phony',
			'rule',
			'CLEAN',
			'rule',
			'HELP',
		],
		description='All primary targets available',
	)

	n.var('build_dir', str(ninja.BUILD_DIR_REL))

	n.build('CLEAN', 'clean').finish()
	n.build('HELP', 'help').finish()

	n.rule('phony_touch', command=['touch', ninja.VAR_OUT])
	n.rule(
		'CUSTOM_COMMAND',
		command=[
			'cd',
			ninja.RawStr('$CWD'),
			ninja.AND,
			ninja.RawStr('$ENV'),
			ninja.RawStr('$COMMAND'),
		],
		description='Running custom command',
	)
	n.rule('cp', command=['cp', ninja.VAR_IN, ninja.VAR_OUT])

	# In-tree genvm-tool launcher that the generated build edges (regen + codegen)
	# shell out to, instead of repeating the `PYTHONPATH=… python -m genvm_tool`
	# incantation. Written directly here (not via a ninja edge) so it exists for the
	# first build. It front-loads the in-tree source on PYTHONPATH — so an edit to
	# cmd_configure.py or a codegen backend takes effect on the next build rather
	# than running a stale installed copy — and pins this env's interpreter (there
	# is no `genvm-tool` wrapper in the source tree, and PATH may not carry it under
	# ninja). `${PYTHONPATH:-}` keeps an unset inherited path empty.
	tool_src = source_dir / 'support' / 'tools' / 'genvm-tool'
	genvm_tool_sh = build_dir / 'genvm_tool.sh'
	build_dir.mkdir(parents=True, exist_ok=True)
	# The in-tree source must come *first*: `sys.path` carries the installed
	# genvm-tool (this process was launched from it), which would otherwise shadow
	# the working tree and make codegen edits silently no-ops.
	pp = ':'.join([str(tool_src)] + sys.path)
	genvm_tool_sh.write_text(
		'#!/bin/sh\n'
		'# Generated by `genvm-tool configure`, DO NOT EDIT MANUALLY.\n'
		f'export PYTHONPATH="{pp}:${{PYTHONPATH:-}}"\n'
		f'exec "{sys.executable}" -m genvm_tool "$@"\n'
	)
	genvm_tool_sh.chmod(0o755)

	# Regenerate build.ninja when this script changes (re-runs the tool via the
	# launcher above). Preserve the configure flags so a regen keeps mode.
	regen_args = []
	if args.ci:
		regen_args.append('--ci')
	regen = n.build('CUSTOM_COMMAND', 'build.ninja')
	regen.add_dependency(tool_src / 'genvm_tool' / 'cmd_configure.py')
	# The build graph is also shaped by the shared ninja plugin and by each line's
	# own `configure` hook, so editing any of them must trigger a regen too.
	regen.add_dependency(source_dir / PLUGINS_REL / 'genvm_tool_plugins' / 'ninja.py')
	# The active version set and each line's pinned version drive the graph too.
	regen.add_dependency(source_dir / MONOREPO_ROOT_FILE)
	for v in versions:
		regen.add_dependency(source_dir / v.exec_rel / 'manifest.json')
		regen.add_dependency(source_dir / v.exec_rel / common.PROJECT_FILE)
	regen.var('COMMAND', [str(genvm_tool_sh), 'configure'] + regen_args)
	regen.var('CWD', str(source_dir))
	regen.finish()

	# Codegen shells out to the same launcher; `$lang` is per-edge, `$in` is the
	# data JSON, `$out` the generated file.
	n.rule(
		'codegen',
		command=[
			str(genvm_tool_sh),
			'codegen',
			'--lang',
			ninja.RawStr('$lang'),
			'--in',
			ninja.VAR_IN,
			'--out',
			ninja.VAR_OUT,
			ninja.RawStr('$extra_flags'),
		],
		description='Codegen $out',
	)
	# Regenerate when any codegen backend changes (the data JSON is a per-edge dep).
	n.codegen_deps = ninja.glob(tool_src / 'genvm_tool' / 'codegen', '*.py') + [
		tool_src / 'genvm_tool' / 'cmd_codegen.py'
	]

	codegen_phony = n.build('phony', 'codegen')

	# Manager-global generated files have a single output. Host protocol data is
	# shared; public ABI data still comes from the primary line.
	p_data = primary_exec_root / 'executor' / 'codegen' / 'data'
	shared_data = source_dir / 'crates/modules-interfaces/codegen/data'
	host_fns_rs = source_dir / 'crates/modules-interfaces/src/host_fns.rs'
	host_fns_py = source_dir / 'tests/runner/origin/host_fns.py'
	manager_api_rs = source_dir / 'crates/modules-interfaces/src/manager_api.rs'
	manager_api_py = source_dir / 'tests/runner/origin/manager_api.py'
	manager_socket_consts_rst = (
		source_dir / 'docs/website/src/impl-spec/appendix/manager-socket-consts.rst'
	)
	public_abi_py = source_dir / 'tests/runner/origin/public_abi.py'
	constants_rst = source_dir / 'docs/website/src/spec/appendix/constants.rst'
	internal_constants_rst = (
		source_dir / 'docs/website/src/spec/appendix/internal-constants.rst'
	)
	# Public-ABI constants staged for a future release: documented in their own
	# appendix page so the spec can reference them, without feeding the
	# runner-hashed `public_abi.py`. Empty while nothing is staged.
	constants_pending_rst = (
		source_dir / 'docs/website/src/spec/appendix/constants-pending.rst'
	)

	n.codegen(host_fns_rs, 'rust', shared_data / 'host-fns.json')
	n.codegen(host_fns_py, 'python', shared_data / 'host-fns.json')
	n.codegen(manager_api_rs, 'rust', shared_data / 'manager-api.json')
	n.codegen(manager_api_py, 'python', shared_data / 'manager-api.json')
	n.codegen(manager_socket_consts_rst, 'rst', shared_data / 'manager-api.json')
	n.codegen(public_abi_py, 'python', p_data / 'public-abi.json')
	n.codegen(constants_rst, 'rst', p_data / 'public-abi.json')
	n.codegen(
		internal_constants_rst,
		'rst',
		p_data / 'internal-constants.json',
	)
	n.codegen(
		constants_pending_rst,
		'rst',
		p_data / 'public-abi-pending.json',
		['--rst-anchor-ns=pending'],
	)
	for out in (
		host_fns_rs,
		host_fns_py,
		manager_api_rs,
		manager_api_py,
		manager_socket_consts_rst,
		public_abi_py,
		constants_rst,
		internal_constants_rst,
		constants_pending_rst,
	):
		codegen_phony.add_dependency(out)

	cargo_cmd = [common.command_to_executable('cargo')]

	# CI mode forbids touching Cargo.lock during the build.
	locked = ['--locked'] if args.ci else []

	# Lint edges (clippy) never create `$out`, so ninja re-runs them every time.
	n.rule(
		'cargo',
		command=[
			'cd',
			ninja.RawStr('$wd'),
			ninja.AND,
			ninja.RawStr(ninja.CARGO_LD_LIBRARY_PATH),
			ninja.RawStr('$env'),
			*cargo_cmd,
			ninja.RawStr('$subcommand'),
			'--target',
			rust_target,
			'--target-dir',
			ninja.RawStr('$target_dir'),
			*locked,
			ninja.RawStr('$extra_args'),
		],
		description='Running cargo $subcommand',
		pool='console',
	)
	n.rule(
		'cargo_build',
		command=[
			'cd',
			ninja.RawStr('$wd'),
			ninja.AND,
			ninja.RawStr(ninja.CARGO_LD_LIBRARY_PATH),
			ninja.RawStr('$env'),
			*cargo_cmd,
			'build',
			'--target',
			rust_target,
			'--target-dir',
			ninja.RawStr('$target_dir'),
			*locked,
			ninja.RawStr('$extra_args'),
		],
		description='Running cargo $subcommand',
		depfile=ninja.RawStr('$out.d'),
		pool='console',
	)

	# Manager-level crates (version-independent).
	n.register_cargo(
		'implementation',
		extra_args=['--features', 'vendored-lua'],
		build_to='out/bin/genvm-modules',
	)
	n.register_cargo('crates/modules-interfaces')

	n.rule(
		'nix_eval',
		command=[
			ninja.RawStr('WD=$$(pwd)'),
			ninja.AND,
			'cd',
			ninja.RawStr('$wd'),
			ninja.AND,
			'nix',
			'eval',
			'--verbose',
			'--impure',
			'--read-only',
			'--show-trace',
			'--json',
			'--expr',
			ninja.RawStr('$expr'),
			ninja.RawStr('>'),
			ninja.RawStr('$$WD/$out'),
		],
		pool='console',
	)

	data_phony = n.build('phony', 'all/data')

	# all/bin: build the binaries and `cp` every install-tree file into out/.
	all_manager = n.build('phony', 'all/manager')
	all_manager.add_dependency('out/bin/genvm-modules')
	all_bin = n.build('phony', 'all/bin')
	all_bin.add_dependency(all_manager)

	# Per-line executor build, runner data, codegen, and install tree, each
	# landing under its own `out/executor/<real-version>` directory. Each line's
	# `.genvm-tool.py:configure(line)` hook owns its registrations (the lines
	# already diverge — e.g. frozen runner registry vs nix-derived manifests).
	for v in versions:
		all_exec_line = n.build('phony', f'all/executor/{v.key}')
		all_bin.add_dependency(all_exec_line)
		line = ninja.LineContext(
			n=n,
			source_dir=source_dir,
			key=v.key,
			exec_rel=v.exec_rel,
			real=v.real,
			codegen_phony=codegen_phony,
			data_phony=data_phony,
			all_bin=all_exec_line,
			is_support_only=v.key in monorepo_cfg.get('support-only-versions', []),
		)
		configurator = _line_configurator(ctx, source_dir, v.exec_rel)
		configurator(line)

		all_exec_line.finish()

	codegen_phony.finish()
	data_phony.finish()

	def _runner_file(f: Path) -> bool:
		return f.is_file() and not ({'test', 'tests', 'fuzz'} & set(f.parts))

	runners = primary_exec_root / 'runners'
	# The executor lines export their current runner lists; the umbrella's
	# ./runners owns the accumulate-and-build machinery (runners-all). Watch both.
	umbrella_runners = source_dir / 'runners'
	runner_inputs = [
		f
		for d in (runners, umbrella_runners)
		for f in ninja.glob(d, '**/*')
		if _runner_file(f)
	]

	n.build('phony', 'all/runners').add_dependency('target/runners').finish()

	runners_build = n.build('CUSTOM_COMMAND', 'target/runners')
	runners_build.var(
		'command',
		[
			'nix',
			'build',
			'--keep-going',
			'-v',
			'-L',
			'-o',
			ninja.BUILD_DIR_REL / 'runners-nix',
			f'git+file:{source_dir}?submodules=1#runners-all',
			ninja.AND,
			'mkdir',
			'-p',
			'./out/runners',
			ninja.AND,
			'cp',
			'-r',
			'./runners-nix/.',
			'./out/runners/.',
			ninja.AND,
			'chmod',
			'-R',
			'+w',
			'./out/runners/.',
			# Legacy lines (v0.2.x) keep their runners under their own executor
			# root (out/executor/<version>/legacy-runners); the nix output is
			# already laid out at that relative path, so overlay it onto ./out.
			ninja.AND,
			'nix',
			'build',
			'--keep-going',
			'-v',
			'-L',
			'-o',
			ninja.BUILD_DIR_REL / 'legacy-runners-nix',
			f'git+file:{source_dir}?submodules=1#legacy-runners-all',
			ninja.AND,
			'cp',
			'-r',
			'./legacy-runners-nix/.',
			'./out/.',
			ninja.AND,
			'chmod',
			'-R',
			'+w',
			'./out/executor',
		],
	)
	runners_build.add_dependency(source_dir / 'flake.nix')
	runners_build.add_implicit_dependency(runner_inputs)
	runners_build.var('pool', 'console')
	runners_build.finish()

	n.build('phony', 'cargo/fmt').add_dependency(n.all_format).finish()
	n.build('phony', 'cargo/clippy').add_dependency(n.all_clippy).finish()
	n.build('phony', 'cargo/clippy/fix').add_dependency(n.all_clippy_fix).finish()

	n.install('install', 'out', all_manager)

	# The LLM dispatch script requires the `llm_policy` package from the
	# unhardcoded-engine submodule; fail loudly if it was not checked out.
	llm_policy_dir = source_dir / 'libs' / 'unhardcoded-engine'
	if not (llm_policy_dir / 'llm_policy.lua').is_file():
		raise FileNotFoundError(
			f'{llm_policy_dir} is missing or incomplete; '
			'run `git submodule update --init libs/unhardcoded-engine`'
		)
	n.install(
		'libs/unhardcoded-engine/llm_policy',
		'out/lib/genvm-lua/llm_policy',
		all_manager,
	)
	llm_policy_entry = 'out/lib/genvm-lua/llm_policy.lua'
	all_manager.add_dependency(llm_policy_entry)
	n.build('cp', llm_policy_entry).add_dependency(
		llm_policy_dir / 'llm_policy.lua'
	).finish()

	all_manager.finish()
	all_bin.finish()

	all_phony = n.build('phony', 'all')
	all_phony.add_dependency('all/bin')
	all_phony.add_dependency('all/data')
	all_phony.add_dependency('target/runners')
	all_phony.finish()

	n.buf.append('default all\n\n')

	(build_dir / 'cov').mkdir(parents=True, exist_ok=True)
	(build_dir / 'build.ninja').write_text(''.join(n.buf))
	info = base_info(monorepo_cfg, source_dir, build_dir, rust_target_dir)
	(build_dir / 'info.json').write_text(json.dumps(info, indent=2))

	# The manager reads out/data/manifest.yaml at runtime; assemble it from the
	# active executor submodules (the same logic release packaging runs via
	# `genvm-tool build-manifest`).
	from . import manifest

	manifest_path = build_dir / 'out' / 'data' / 'manifest.yaml'
	manifest.write(source_dir, manifest_path)

	ctx.printer.put(
		'configured',
		build_ninja=str(build_dir / 'build.ninja'),
		info=str(build_dir / 'info.json'),
		manifest=str(manifest_path),
		rust_target=rust_target,
	)
	return 0
