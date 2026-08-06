"""Ninja build-graph DSL + per-line `configure` helpers (importable plugin).

Extracted from ``genvm-tool configure`` so each executor line can own its own
build configuration. The shared machinery lives here — the ninja DSL
(:class:`Ninja` / :class:`Build`) and the *standard* per-line registrations
(cargo crates, `genvm-tool codegen`, install tree) bundled on :class:`LineContext`.

The configure command builds one :class:`LineContext` per active line and hands
it to that line's ``.genvm-tool.py:configure(line)`` hook. A line's hook composes
the standard steps and picks how to resolve the runner registry — the one place
the lines already diverge: a frozen line copies its committed
``executor/registry`` verbatim (:meth:`LineContext.frozen_registry`), a live line
derives the manifests through the umbrella nix machinery
(:meth:`LineContext.nix_manifests`). New incompatibilities are added as line
hooks here, never as ``if version ==`` branches in the command.

This is a plain importable library (per the plugin philosophy); it is pulled in
via ``extra_python_paths`` and used both by the command and by the executor
hooks. Stdlib-only, so it keeps ``configure`` free of the test dependency closure.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

# Build directory, manager-root-relative. Kept as a relative path because the
# generated `build_dir` ninja variable must read `build`, not an absolute path.
BUILD_DIR_REL = Path('build')

# Cargo needs the rust toolchain's libs on LD_LIBRARY_PATH; `$$` escapes the `$`
# from ninja so the shell expands it, `:+` keeps an unset LD_LIBRARY_PATH empty.
CARGO_LD_LIBRARY_PATH = (
	'LD_LIBRARY_PATH="$${CARGO_LD_LIBRARY_PATH}$${LD_LIBRARY_PATH:+:$$LD_LIBRARY_PATH}"'
)


def target_dir_for_line(rust_target_dir: Path, key: str) -> Path:
	"""Cargo target dir for one executor line.

	Lines ship crates with identical names and versions; cargo's artifact hash
	does not cover where a package came from, so in a shared target dir two lines
	write the same ``libfoo-<hash>.rlib`` and silently overwrite each other.
	Everything that runs cargo resolves through here.
	"""
	return rust_target_dir / key


# --- ninja DSL -------------------------------------------------------------


class RawStr(str):
	"""A string emitted verbatim into the ninja file (never shell-escaped)."""


AND = RawStr('&&')
VAR_IN = RawStr('$in')
VAR_OUT = RawStr('$out')

# Env for every cargo edge. Coverage instrumentation is deliberately absent:
# only `genvm-tool test run --coverage` builds instrumented, and it uses its own
# RUSTFLAGS. Instrumenting here cost ~2x in every wasm compilation the debug
# executor performs (`precompile`, first run of a contract) while the profile it
# produced went to /dev/null.
CARGO_ENV = RawStr('LLVM_PROFILE_FILE=/dev/null')

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


def glob(base: Path, pattern: str) -> list[Path]:
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


def relpath(target: Path, start: Path) -> str:
	return os.path.relpath(str(target), str(start))


def is_subpath(path: Path, base: Path) -> bool:
	return not relpath(path, base).startswith('..')


# Nix expressions that evaluate a checkout's runner derivations to their
# {id: hash} (latest.json) and {id: [hash]} (all.json) maps.
def runner_manifest_expr(version: str, kind: str) -> str:
	"""Nix expr for one executor's `latest`/`all` runner manifest.

	The umbrella's ./runners owns this: `all` is every runner compatible up to
	`version`, `latest` is that executor's own current runners. Evaluated with
	cwd at the manager root (the nix_eval rule cds into ``$wd``).
	"""
	return (
		f'(import ./runners/manifest.nix '
		f'{{ executorVersion = "{version}"; host-system = builtins.currentSystem; }}).{kind}'
	)


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
			return relpath(self.source_dir / value, self.build_dir)
		if is_subpath(value, self.build_dir) or is_subpath(value, self.source_dir):
			return relpath(value, self.build_dir)
		return str(value)

	def _scalar(self, value) -> None:
		if isinstance(value, Build):
			# A Build used as an output/dependency reference means "this edge's
			# output" — depend on its primary output (which for a multi-output edge
			# is enough to force the whole edge; aggregator phony edges have one).
			# Without this a Build passed to add_dependency would leak its repr.
			assert value.outputs, 'cannot reference a Build that has no output'
			self._scalar(value.outputs[0])
		elif isinstance(value, RawStr):
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

	def codegen(
		self, out: Path, lang: str, data: Path, extra_flags: list[str] | None = None
	) -> None:
		"""Emit one `codegen` edge (`genvm-tool codegen --lang <lang>`).

		Requires the `codegen` rule. The generated file depends on its data JSON and
		on the codegen backend sources (``codegen_deps``, set by the command) so an
		edit to either regenerates it. ``extra_flags`` are appended to the command
		verbatim (e.g. ``--rst-anchor-ns``).
		"""
		b = self.build('codegen', out)
		b.add_dependency(data)
		b.add_implicit_dependency(getattr(self, 'codegen_deps', []))
		b.var('lang', lang)
		# `var` shell-quotes its value, so an empty one would become a literal ''
		# argument on every codegen edge.
		if extra_flags:
			b.var('extra_flags', ' '.join(extra_flags))
		b.finish()

	def install(self, frm: str, to: str, phony: Build) -> None:
		"""`cp` every file under `frm` into `to`, registering each on `phony`."""
		install_dir = self.source_dir / frm
		for f in glob(install_dir, '**/*'):
			if f.is_dir():
				continue
			out = to + '/' + str(f.relative_to(install_dir))
			phony.add_dependency(out)
			self.build('cp', out).add_dependency(f).finish()

	# --- per-crate cargo edges --------------------------------------------

	def register_cargo(
		self,
		rel_path: str,
		extra_args=(),
		build_to: str | None = None,
		target_dir: Path | None = None,
	) -> None:
		extra_args = list(extra_args)
		# Per-line cargo target dir; falls back to the shared default. Executor
		# lines pass their own so two `genvm` binaries don't share `debug/genvm`.
		td = target_dir if target_dir is not None else self.rust_target_dir
		to = BUILD_DIR_REL / rel_path
		(self.source_dir / to).mkdir(parents=True, exist_ok=True)

		crate_dir = Path(rel_path)
		base = self.source_dir / rel_path
		all_files = [crate_dir / p.relative_to(base) for p in glob(base, '**/*.rs')]
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
		clippy.var('env', CARGO_ENV)
		if target_dir is not None:
			clippy.var('target_dir', str(td))
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
		fix.var('env', CARGO_ENV)
		if target_dir is not None:
			fix.var('target_dir', str(td))
		fix.finish()
		self.all_clippy_fix.append('target/' + rel_path + '/clippy/fix')

		fmt = self.build('CUSTOM_COMMAND', 'target/' + rel_path + '/fmt')
		fmt.var('command', ['cd', crate_dir, AND, 'cargo', 'fmt'])
		fmt.description('Run cargo fmt for ' + rel_path)
		fmt.finish()
		self.all_format.append('target/' + rel_path + '/fmt')

		if build_to is not None:
			bin_name = str(td / self.rust_target / 'debug' / build_to.split('/')[-1])
			build = self.build('cargo_build', bin_name)
			build.add_dependency(files_trg)
			build.var('wd', crate_dir)
			build.var('extra_args', extra_args)
			build.var('env', CARGO_ENV)
			if target_dir is not None:
				build.var('target_dir', str(td))
			build.finish()

			self.build('cp', build_to).add_dependency(bin_name).finish()


# --- per-executor-line configuration --------------------------------------


@dataclass
class LineContext:
	"""Everything one executor line's ``configure(line)`` hook needs.

	Built by the configure command per active line. The hook composes the
	``register_standard_*`` / ``install_tree`` steps and chooses a runner-registry
	resolver (:meth:`frozen_registry` vs :meth:`nix_manifests`). The phony nodes
	are shared, command-owned aggregates the hook appends to; the command finishes
	them after every line's hook has run.
	"""

	n: Ninja
	source_dir: Path
	key: str  # version line, e.g. 'v0.3'
	exec_rel: str  # 'executors/v0.3.x'
	real: str  # concrete pinned version, e.g. 'v0.3.0-rc7'
	codegen_phony: Build
	data_phony: Build
	all_bin: Build

	is_support_only: bool

	@property
	def exec_root(self) -> Path:
		return self.source_dir / self.exec_rel

	@property
	def out_exec(self) -> str:
		"""Install prefix for this line: ``out/executor/<real-version>``."""
		return f'out/executor/{self.real}'

	@property
	def line_target_dir(self) -> Path:
		"""This line's cargo target dir; see :func:`target_dir_for_line`."""
		return target_dir_for_line(self.n.rust_target_dir, self.key)

	def codegen(
		self, out: Path, lang: str, data: Path, extra_flags: list[str] | None = None
	) -> None:
		"""Emit a codegen edge and register its output on the codegen phony."""
		self.n.codegen(out, lang, data, extra_flags)
		self.codegen_phony.add_dependency(out)

	def register_standard_codegen(self) -> None:
		"""Per-line generated files inside this line's executor/runner source tree."""
		data = self.exec_root / 'executor' / 'codegen' / 'data'
		self.codegen(
			self.exec_root / 'executor/crates/sdk-rs/src/abi/consts.rs',
			'rust',
			data / 'public-abi.json',
		)
		self.codegen(
			self.exec_root / 'runners/genlayer-py-std/src/genlayer/vm/public_abi.py',
			'python',
			data / 'public-abi.json',
		)
		pending_abi = self.exec_root / 'executor/codegen/data/public-abi-pending.json'
		if pending_abi.exists():
			self.codegen(
				self.exec_root / 'executor/crates/common/src/public_abi_pending.rs',
				'rust',
				self.exec_root / 'executor/codegen/data/public-abi-pending.json',
			)

	def register_standard_crates(self) -> None:
		"""Build this line's `genvm` binary plus its `common`/`sdk-rs` crates.

		`calldata` and `calldata-derive` live in this line's tree too but get no
		edge of their own — they are path deps of the crates listed here.
		"""
		td = self.line_target_dir
		self.n.register_cargo(
			f'{self.exec_rel}/executor',
			build_to=f'{self.out_exec}/bin/genvm',
			target_dir=td,
		)
		self.n.register_cargo(f'{self.exec_rel}/executor/crates/common', target_dir=td)
		self.n.register_cargo(f'{self.exec_rel}/executor/crates/sdk-rs', target_dir=td)

	def frozen_registry(self) -> None:
		"""Copy this line's committed ``executor/registry`` runner manifests verbatim.

		Frozen legacy lines ship the exact ``latest.json``/``all.json`` from their
		release, which the nix build copies verbatim (see that line's
		``executor/default.nix``). The debug build must use the same files, not
		recompute via the umbrella machinery, or the two builds disagree on runner
		hashes.
		"""
		registry_dir = self.exec_root / 'executor' / 'registry'
		for name in ('latest.json', 'all.json'):
			out = f'{self.out_exec}/data/{name}'
			self.n.build('cp', out).add_dependency(registry_dir / name).finish()
			self.data_phony.add_dependency(out)

	def nix_manifests(self) -> None:
		"""Derive this line's `latest`/`all` runner manifests via the nix machinery.

		Manifests are derived by the umbrella machinery (which imports every active
		line's runners), so both the line's and the umbrella's nix inputs matter.
		Requires the `nix_eval` rule.
		"""
		runners_nix_inputs = glob(self.exec_root / 'runners', '**/*.nix') + glob(
			self.source_dir / 'runners', '**/*.nix'
		)
		for kind in ('latest', 'all'):
			out = f'{self.out_exec}/data/{kind}.json'
			edge = self.n.build('nix_eval', out)
			edge.var('expr', runner_manifest_expr(self.real, kind))
			edge.var('wd', self.source_dir)
			edge.add_implicit_dependency(runners_nix_inputs)
			edge.finish()
			self.data_phony.add_dependency(out)

	def install_tree(self) -> None:
		"""`cp` this line's install tree into `out_exec` and register its binary."""
		self.n.install(f'{self.exec_rel}/executor/install', self.out_exec, self.all_bin)
		self.all_bin.add_dependency(f'{self.out_exec}/bin/genvm')


def configure_line_default(line: LineContext) -> None:
	"""Fallback per-line configuration for a line whose `.genvm-tool.py` has no
	`configure` hook: the standard codegen + crates + install, resolving the
	runner registry from a committed ``executor/registry`` when present, else nix.
	"""
	line.register_standard_codegen()
	line.register_standard_crates()
	if (line.exec_root / 'executor' / 'registry' / 'all.json').is_file():
		line.frozen_registry()
	else:
		line.nix_manifests()
	line.install_tree()
