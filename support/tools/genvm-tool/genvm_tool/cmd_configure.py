"""`genvm-tool configure` — generate build/build.ninja and build/info.json.

Generates the ninja build graph that drives cargo (build / clippy / clippy-fix /
fmt), the ruby codegen templates, the nix runner build, and the install `cp`
steps. (This was historically the manager-root `configure.rb` Ruby script.)

Every path is resolved relative to the manager root (`ctx.root`), so the graph is
identical regardless of the directory the tool is invoked from — the original
script assumed it was run from the source root, which is the same thing once the
root is located.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from . import common

NAME = 'configure'
HELP = 'generate build/build.ninja and build/info.json'

# Umbrella manifest listing the active top-level version lines and the primary.
MONOREPO_ROOT_FILE = '.genvm-monorepo-root'


class ExecVersion(NamedTuple):
	"""One active executor line resolved to its on-disk locations.

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


def _load_versions(source_dir: Path) -> tuple[str, list[ExecVersion]]:
	"""Resolve every active executor line from ``.genvm-monorepo-root``.

	Each active line is mounted at ``executors/<line>.x`` and its
	``manifest.json`` pins the concrete ``executor-version`` that becomes the
	built ``out/executor/<real>`` directory. There may be several active lines.
	"""
	cfg = json.loads((source_dir / MONOREPO_ROOT_FILE).read_text())
	versions = []
	for key in cfg['active-versions']:
		exec_rel = f'executors/{key}.x'
		manifest = json.loads((source_dir / exec_rel / 'manifest.json').read_text())
		versions.append(
			ExecVersion(key=key, exec_rel=exec_rel, real=manifest['executor-version'])
		)
	return cfg['version'], versions


# Build directory, manager-root-relative. Kept as a relative path because the
# generated `build_dir` ninja variable must read `build`, not an absolute path.
BUILD_DIR_REL = Path('build')

# Cargo needs the rust toolchain's libs on LD_LIBRARY_PATH; `$$` escapes the `$`
# from ninja so the shell expands it, `:+` keeps an unset LD_LIBRARY_PATH empty.
CARGO_LD_LIBRARY_PATH = (
	'LD_LIBRARY_PATH="$${CARGO_LD_LIBRARY_PATH}$${LD_LIBRARY_PATH:+:$$LD_LIBRARY_PATH}"'
)


def configure(parser):
	parser.add_argument(
		'--test-always-fail-module',
		action='store_true',
		help='enable the test_always_fail_module feature on the executor',
	)
	parser.add_argument(
		'--ci',
		action='store_true',
		help='CI mode: pass --locked to cargo so the build fails on a stale Cargo.lock',
	)


# --- ninja DSL -------------------------------------------------------------


class RawStr(str):
	"""A string emitted verbatim into the ninja file (never shell-escaped)."""


AND = RawStr('&&')
VAR_IN = RawStr('$in')
VAR_OUT = RawStr('$out')

# clippy/build runs are instrumented for coverage but discard the profile here.
COVERAGE_ENV = RawStr('RUSTFLAGS="-C instrument-coverage" LLVM_PROFILE_FILE=/dev/null')

# Ruby's Shellwords.escape leaves this set unescaped; everything else (including
# spaces, quotes, `$`, `?`, `#`) gets a leading backslash.
_UNSAFE = re.compile(r'[^A-Za-z0-9_\-.,:+/@\n]')


def _shellescape(s: str) -> str:
	if s == '':
		return "''"
	s = _UNSAFE.sub(lambda m: '\\' + m.group(0), s)
	# A backslash before LF is a line continuation, so wrap newlines in quotes.
	return s.replace('\n', "'\n'")


def _escape(s: str) -> str:
	# configure.rb un-escapes `\=`, keeping `FOO=bar` env assignments readable.
	return _shellescape(s).replace('\\=', '=')


def _glob(base: Path, pattern: str) -> list[Path]:
	"""Mirror Ruby's `Pathname#glob`: a depth-first, per-component sorted walk
	that skips dotfile entries (no FNM_DOTMATCH).

	Sorting by path *components* (not the joined string) is what makes
	`calldata/...` precede `calldata-derive/...`: the `/` separator dominates,
	exactly as Ruby's recursive directory traversal does.
	"""
	out = []
	for p in base.glob(pattern):
		rel = p.relative_to(base)
		if any(part.startswith('.') for part in rel.parts):
			continue
		out.append(p)
	return sorted(out, key=lambda p: p.parts)


def _relpath(target: Path, start: Path) -> str:
	return os.path.relpath(str(target), str(start))


def _is_subpath(path: Path, base: Path) -> bool:
	return not _relpath(path, base).startswith('..')


class Build:
	"""One `build` edge; mirrors configure.rb's BuildBuilder."""

	def __init__(self, ninja: 'Ninja', rule: str, outputs):
		self.ninja = ninja
		self.rule = rule
		self.outputs = list(outputs)
		self.implicit_outputs: list = []
		self.deps: list = []
		self.implicit_deps: list = []
		self.order_only_deps: list = []
		self.props: dict = {}

	@staticmethod
	def _add(arr: list, item) -> None:
		if isinstance(item, (list, tuple)):
			arr.extend(item)
		else:
			arr.append(item)

	def add_output(self, output):
		self._add(self.outputs, output)
		return self

	def add_implicit_output(self, output):
		self._add(self.implicit_outputs, output)
		return self

	def add_dependency(self, dep):
		self._add(self.deps, dep)
		return self

	def add_implicit_dependency(self, dep):
		self._add(self.implicit_deps, dep)
		return self

	def add_order_only_dependency(self, dep):
		self._add(self.order_only_deps, dep)
		return self

	def var(self, name: str, value):
		self.props[name] = value
		return self

	def description(self, desc: str):
		self.props['description'] = desc
		return self

	def finish(self) -> None:
		assert self.outputs, 'build edge must have at least one output'
		n = self.ninja
		n.buf.append('build')
		for o in self.outputs:
			n.buf.append(' ')
			n._scalar(o)
		if self.implicit_outputs:
			n.buf.append(' |')
			for o in self.implicit_outputs:
				n.buf.append(' ')
				n._scalar(o)
		n.buf.append(': ')
		n.buf.append(self.rule)
		for d in self.deps:
			n.buf.append(' ')
			n._scalar(d)
		if self.implicit_deps:
			n.buf.append(' |')
			for d in self.implicit_deps:
				n.buf.append(' ')
				n._scalar(d)
		if self.order_only_deps:
			n.buf.append(' ||')
			for d in self.order_only_deps:
				n.buf.append(' ')
				n._scalar(d)
		n.buf.append('\n')
		for key, value in self.props.items():
			n.buf.append(f'  {key} = ')
			n._value(value)
			n.buf.append('\n')
		n.buf.append('\n')


class Ninja:
	"""Accumulates the build.ninja text; mirrors configure.rb's Ninja::File."""

	def __init__(self, source_dir: Path, build_dir: Path):
		self.source_dir = source_dir
		self.build_dir = build_dir  # absolute; this is ninja's working directory
		self.buf: list[str] = []
		self.all_format: list[str] = []
		self.all_clippy: list[str] = []
		self.all_clippy_fix: list[str] = []
		self.rust_target = ''
		self.rust_target_dir = build_dir  # set by main
		build_dir.mkdir(parents=True, exist_ok=True)

	def _resolve_path(self, value: Path) -> str:
		"""Render a Path as ninja sees it: relative to the build (working) dir."""
		if not value.is_absolute():
			# Relative paths are interpreted against the source root.
			return _relpath(self.source_dir / value, self.build_dir)
		if _is_subpath(value, self.build_dir) or _is_subpath(value, self.source_dir):
			return _relpath(value, self.build_dir)
		return str(value)

	def _scalar(self, value) -> None:
		if isinstance(value, RawStr):
			self.buf.append(str(value))
		elif isinstance(value, Path):
			self.buf.append(_escape(self._resolve_path(value)))
		else:
			self.buf.append(_escape(str(value)))

	def _value(self, value) -> None:
		if isinstance(value, (list, tuple)):
			for i, v in enumerate(value):
				if i:
					self.buf.append(' ')
				self._value(v)
		else:
			self._scalar(value)

	def comment(self, text: str) -> None:
		for line in text.splitlines():
			self.buf.append(f'# {line.strip()}\n')

	def var(self, name: str, value) -> None:
		self.buf.append(f'{name} = ')
		self._value(value)
		self.buf.append('\n\n')

	def rule(self, name: str, **props) -> None:
		assert 'command' in props, f'rule {name} must have a command'
		self.buf.append(f'rule {name}\n')
		for key, value in props.items():
			self.buf.append(f'  {key} = ')
			self._value(value)
			self.buf.append('\n')
		self.buf.append('\n')

	def build(self, rule: str, *outputs) -> Build:
		return Build(self, rule, outputs)

	# --- per-crate cargo edges --------------------------------------------

	def register_cargo(
		self, rel_path: str, extra_args=(), build_to: str | None = None
	) -> None:
		extra_args = list(extra_args)
		to = BUILD_DIR_REL / rel_path
		(self.source_dir / to).mkdir(parents=True, exist_ok=True)

		crate_dir = Path(rel_path)
		base = self.source_dir / rel_path
		all_files = [crate_dir / p.relative_to(base) for p in _glob(base, '**/*.rs')]
		all_files += [crate_dir / 'Cargo.toml', crate_dir / 'Cargo.lock']

		files_trg = BUILD_DIR_REL / 'ya-build' / rel_path / 'files.trg'
		(self.source_dir / files_trg).parent.mkdir(parents=True, exist_ok=True)

		self.build('phony_touch', files_trg).add_implicit_dependency(all_files).finish()

		clippy_lints = ['--', '-A', 'clippy::upper_case_acronyms', '-Dwarnings']

		clippy = self.build('cargo', 'target/' + rel_path + '/clippy')
		clippy.add_dependency(files_trg)
		clippy.var('subcommand', 'clippy')
		clippy.var('wd', crate_dir)
		clippy.var('extra_args', extra_args + clippy_lints)
		clippy.var('env', COVERAGE_ENV)
		clippy.description('Run cargo clippy for ' + rel_path)
		clippy.finish()
		self.all_clippy.append('target/' + rel_path + '/clippy')

		fix = self.build('cargo', 'target/' + rel_path + '/clippy/fix')
		fix.var('subcommand', 'clippy')
		fix.var('wd', crate_dir)
		fix.var(
			'extra_args',
			extra_args + ['--fix', '--allow-dirty', '--allow-staged'] + clippy_lints,
		)
		fix.var('env', COVERAGE_ENV)
		fix.finish()
		self.all_clippy_fix.append('target/' + rel_path + '/clippy/fix')

		fmt = self.build('CUSTOM_COMMAND', 'target/' + rel_path + '/fmt')
		fmt.var('command', ['cd', crate_dir, AND, 'cargo', 'fmt'])
		fmt.description('Run cargo fmt for ' + rel_path)
		fmt.finish()
		self.all_format.append('target/' + rel_path + '/fmt')

		if build_to is not None:
			bin_name = str(
				self.rust_target_dir / self.rust_target / 'debug' / build_to.split('/')[-1]
			)
			build = self.build('cargo_build', bin_name)
			build.add_dependency(files_trg)
			build.var('wd', crate_dir)
			build.var('extra_args', extra_args)
			build.var('env', COVERAGE_ENV)
			build.finish()

			self.build('cp', build_to).add_dependency(bin_name).finish()


def _detect_rust_target() -> str:
	out = subprocess.run(
		['rustc', '-vV'], capture_output=True, text=True, check=True
	).stdout
	for line in out.splitlines():
		if line.startswith('host: '):
			return line[len('host: ') :].strip()
	raise common.ToolError("failed to detect rust target from 'rustc -vV' output")


# Nix expressions that evaluate a checkout's runner derivations to their
# {id: hash} (latest.json) and {id: [hash]} (all.json) maps.
def _runner_manifest_expr(version: str, kind: str) -> str:
	"""Nix expr for one executor's `latest`/`all` runner manifest.

	The umbrella's ./runners owns this: `all` is every runner compatible up to
	`version`, `latest` is that executor's own current runners. Evaluated with
	cwd at the manager root (the nix_eval rule cds into ``$wd``).
	"""
	return (
		f'(import ./runners/manifest.nix '
		f'{{ executorVersion = "{version}"; host-system = builtins.currentSystem; }}).{kind}'
	)


def main(ctx: common.Context, args) -> int:
	source_dir = ctx.root
	build_dir = source_dir / BUILD_DIR_REL

	primary_key, versions = _load_versions(source_dir)
	# Manager-global generated files (test fixtures, docs) have a single output,
	# so they are derived from the primary line's codegen data.
	primary = next((v for v in versions if v.key == primary_key), versions[0])
	primary_exec_root = source_dir / primary.exec_rel

	rust_target = _detect_rust_target()
	rust_target_dir = build_dir / 'ya-build' / 'rust-target'
	rust_target_dir.mkdir(parents=True, exist_ok=True)

	n = Ninja(source_dir, build_dir)
	n.rust_target = rust_target
	n.rust_target_dir = rust_target_dir

	n.comment('Generated by `genvm-tool configure`, DO NOT EDIT MANUALLY')
	n.var('ninja_required_version', '1.5')

	n.rule(
		'CLEAN',
		command=['ninja', RawStr('$FILE_ARG'), '-t', 'clean', RawStr('$TARGETS')],
		description='Cleaning all built files...',
	)
	n.rule(
		'HELP',
		command=[
			'ninja',
			RawStr('$FILE_ARG'),
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

	n.var('build_dir', str(BUILD_DIR_REL))

	n.build('CLEAN', 'clean').finish()
	n.build('HELP', 'help').finish()

	n.rule('phony_touch', command=['touch', VAR_OUT])
	n.rule(
		'CUSTOM_COMMAND',
		command=['cd', RawStr('$CWD'), AND, RawStr('$ENV'), RawStr('$COMMAND')],
		description='Running custom command',
	)
	n.rule('cp', command=['cp', VAR_IN, VAR_OUT])

	# Regenerate build.ninja when this script changes (re-runs the tool).
	# Re-invoke this exact program via its interpreter + module rather than a
	# `genvm-tool` wrapper (there is none in the source tree, and PATH may not
	# carry it under ninja). Preserve the configure flags so a regen keeps mode.
	genvm_tool = [sys.executable, '-m', 'genvm_tool']
	regen_args = []
	if args.test_always_fail_module:
		regen_args.append('--test-always-fail-module')
	if args.ci:
		regen_args.append('--ci')
	regen = n.build('CUSTOM_COMMAND', 'build.ninja')
	regen.add_dependency(
		source_dir / 'support' / 'tools' / 'genvm-tool' / 'genvm_tool' / 'cmd_configure.py'
	)
	# The active version set and each line's pinned version drive the graph too.
	regen.add_dependency(source_dir / MONOREPO_ROOT_FILE)
	for v in versions:
		regen.add_dependency(source_dir / v.exec_rel / 'manifest.json')
	regen.var('COMMAND', genvm_tool + ['configure'] + regen_args)
	regen.var('CWD', str(source_dir))
	regen.finish()

	n.rule('codegen', command=['ruby', VAR_IN, VAR_OUT])

	def codegen(out: Path, template: Path, data: Path) -> None:
		b = n.build('codegen', out)
		b.add_dependency(template)
		b.add_dependency(data)
		b.finish()

	codegen_phony = n.build('phony', 'codegen')

	# Manager-global generated files (test fixtures + docs) have a single output,
	# so generate them from the primary line's codegen data.
	p_tmpl = primary_exec_root / 'executor' / 'codegen' / 'templates'
	p_data = primary_exec_root / 'executor' / 'codegen' / 'data'
	host_fns_py = source_dir / 'tests/runner/origin/host_fns.py'
	public_abi_py = source_dir / 'tests/runner/origin/public_abi.py'
	constants_rst = source_dir / 'docs/website/src/spec/appendix/constants.rst'

	codegen(host_fns_py, p_tmpl / 'py.rb', p_data / 'host-fns.json')
	codegen(public_abi_py, p_tmpl / 'py.rb', p_data / 'public-abi.json')
	codegen(constants_rst, p_tmpl / 'rst.rb', p_data / 'public-abi.json')
	for out in (host_fns_py, public_abi_py, constants_rst):
		codegen_phony.add_dependency(out)

	# Per-line generated files live inside each executor/runner source tree.
	for v in versions:
		exec_root = source_dir / v.exec_rel
		tmpl = exec_root / 'executor' / 'codegen' / 'templates'
		data = exec_root / 'executor' / 'codegen' / 'data'
		consts_rs = exec_root / 'executor/crates/sdk-rs/src/abi/consts.rs'
		py_std_abi = exec_root / 'runners/genlayer-py-std/src/genlayer/vm/public_abi.py'
		host_fns_rs = exec_root / 'executor/crates/common/src/host_fns.rs'

		codegen(consts_rs, tmpl / 'rs.rb', data / 'public-abi.json')
		codegen(py_std_abi, tmpl / 'py.rb', data / 'public-abi.json')
		codegen(host_fns_rs, tmpl / 'rs.rb', data / 'host-fns.json')
		for out in (consts_rs, py_std_abi, host_fns_rs):
			codegen_phony.add_dependency(out)

	codegen_phony.finish()

	cargo_cmd = [common.command_to_executable('cargo')]

	# CI mode forbids touching Cargo.lock during the build.
	locked = ['--locked'] if args.ci else []

	n.rule(
		'cargo',
		command=[
			'cd',
			RawStr('$wd'),
			AND,
			RawStr(CARGO_LD_LIBRARY_PATH),
			RawStr('$env'),
			*cargo_cmd,
			RawStr('$subcommand'),
			'--target',
			rust_target,
			'--target-dir',
			str(rust_target_dir),
			*locked,
			RawStr('$extra_args'),
			AND,
			'cd',
			str(build_dir),
			AND,
			'touch',
			VAR_OUT,
		],
		description='Running cargo $subcommand',
		pool='console',
	)
	n.rule(
		'cargo_build',
		command=[
			'cd',
			RawStr('$wd'),
			AND,
			RawStr(CARGO_LD_LIBRARY_PATH),
			RawStr('$env'),
			*cargo_cmd,
			'build',
			'--target',
			rust_target,
			'--target-dir',
			str(rust_target_dir),
			*locked,
			RawStr('$extra_args'),
		],
		description='Running cargo $subcommand',
		depfile=RawStr('$out.d'),
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
			RawStr('WD=$$(pwd)'),
			AND,
			'cd',
			RawStr('$wd'),
			AND,
			'nix',
			'eval',
			'--verbose',
			'--impure',
			'--read-only',
			'--show-trace',
			'--json',
			'--expr',
			RawStr('$expr'),
			RawStr('>'),
			RawStr('$$WD/$out'),
		],
		pool='console',
	)

	data_phony = n.build('phony', 'all/data')

	# all/bin: build the binaries and `cp` every install-tree file into out/.
	all_bin = n.build('phony', 'all/bin')
	all_bin.add_dependency('out/bin/genvm-modules')

	def install(frm: str, to: str) -> None:
		install_dir = source_dir / frm
		for f in _glob(install_dir, '**/*'):
			if f.is_dir():
				continue
			out = to + '/' + str(f.relative_to(install_dir))
			all_bin.add_dependency(out)
			n.build('cp', out).add_dependency(f).finish()

	# Per-line executor build, runner data, and install tree, each landing under
	# its own `out/executor/<real-version>` directory.
	for v in versions:
		exec_root = source_dir / v.exec_rel
		out_exec = f'out/executor/{v.real}'

		n.register_cargo(f'{v.exec_rel}/executor', build_to=f'{out_exec}/bin/genvm')
		n.register_cargo(f'{v.exec_rel}/executor/crates/calldata')
		n.register_cargo(f'{v.exec_rel}/executor/crates/calldata-derive')
		n.register_cargo(f'{v.exec_rel}/executor/crates/common')
		n.register_cargo(f'{v.exec_rel}/executor/crates/sdk-rs')

		# Manifests are derived by the umbrella machinery (which imports every
		# active line's runners), so both the line's and the umbrella's nix
		# inputs matter.
		runners_nix_inputs = _glob(exec_root / 'runners', '**/*.nix') + _glob(
			source_dir / 'runners', '**/*.nix'
		)

		latest = n.build('nix_eval', f'{out_exec}/data/latest.json')
		latest.var('expr', _runner_manifest_expr(v.real, 'latest'))
		latest.var('wd', source_dir)
		latest.add_implicit_dependency(runners_nix_inputs)
		latest.finish()

		all_json = n.build('nix_eval', f'{out_exec}/data/all.json')
		all_json.var('expr', _runner_manifest_expr(v.real, 'all'))
		all_json.var('wd', source_dir)
		all_json.add_implicit_dependency(runners_nix_inputs)
		all_json.finish()

		data_phony.add_dependency(f'{out_exec}/data/latest.json')
		data_phony.add_dependency(f'{out_exec}/data/all.json')

		install(f'{v.exec_rel}/executor/install', out_exec)
		all_bin.add_dependency(f'{out_exec}/bin/genvm')

	data_phony.finish()

	def _runner_file(f: Path) -> bool:
		return f.is_file() and not ({'test', 'tests', 'fuzz'} & set(f.parts))

	runners = primary_exec_root / 'runners'
	# The executor lines export their current runner lists; the umbrella's
	# ./runners owns the accumulate-and-build machinery (runners-all). Watch both.
	umbrella_runners = source_dir / 'runners'
	runner_inputs = [
		f for d in (runners, umbrella_runners) for f in _glob(d, '**/*') if _runner_file(f)
	]

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
			BUILD_DIR_REL / 'runners-nix',
			f'git+file:{source_dir}?submodules=1#runners-all',
			AND,
			'mkdir',
			'-p',
			'./out/runners',
			AND,
			'cp',
			'-r',
			'./runners-nix/.',
			'./out/runners/.',
			AND,
			'chmod',
			'-R',
			'+w',
			'./out/runners/.',
		],
	)
	runners_build.add_dependency(source_dir / 'flake.nix')
	runners_build.add_implicit_dependency(runner_inputs)
	runners_build.var('pool', 'console')
	runners_build.finish()

	n.build('phony', 'cargo/fmt').add_dependency(n.all_format).finish()
	n.build('phony', 'cargo/clippy').add_dependency(n.all_clippy).finish()
	n.build('phony', 'cargo/clippy/fix').add_dependency(n.all_clippy_fix).finish()

	install('install', 'out')
	all_bin.finish()

	all_phony = n.build('phony', 'all')
	all_phony.add_dependency('all/bin')
	all_phony.add_dependency('all/data')
	all_phony.add_dependency('target/runners')
	all_phony.finish()

	n.buf.append('default all\n\n')

	(build_dir / 'cov').mkdir(parents=True, exist_ok=True)
	(build_dir / 'build.ninja').write_text(''.join(n.buf))
	info = {
		'coverage_dir': str(build_dir / 'cov'),
		'build_dir': str(build_dir),
		'rust_target_dir': str(rust_target_dir),
		# Maps each active line (e.g. `v0.3`) to its built `out/executor/<real>`
		# directory name. Consumers (test harness) read this to reroute runs to a
		# locally-built executor.
		'executor_versions': {v.key: v.real for v in versions},
		'primary_executor_version': primary.real,
	}
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
