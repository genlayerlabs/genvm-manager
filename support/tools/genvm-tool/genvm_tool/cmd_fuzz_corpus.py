"""
`genvm-tool fuzz-corpus` — seed the fuzz corpora from a finished test run.

The integration suite encodes real calldata, reads real contract code and
collects real leader results on every run, then leaves them in the artifacts
dir. This walks that dir, maps each blob to the fuzz targets that consume its
format, and writes the new ones into the committed `fuzz/inputs-<target>`
corpora, named by content hash exactly as `test run --fuzz-update-corpus` does.

Meant to be run by hand after a suite run, and followed by a fuzz run with
`--fuzz-update-corpus`: that one `cmin`s the union back down before the corpus
is committed.
"""

import dataclasses
import json
import os
import pickle
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

from . import common, misc

NAME = 'fuzz-corpus'
HELP = 'seed the fuzz corpora from a finished test run'

# `arbitrary::Unstructured::int_in_range` eats the first byte and takes it modulo
# the number of variants, so a leading zero selects the variant that hands the
# rest of the input to the parser verbatim. A target taking `|data: &[u8]|`
# parses the whole input and needs no such byte.
_RAW = b'\x00'


@dataclasses.dataclass(frozen=True)
class Target:
	"""A fuzz target that accepts bytes a run produced, and how to frame them."""

	crate: str
	name: str
	prefix: bytes = b''


# What each kind of harvested blob is a seed for. Only the targets that take a
# format are listed: a `-structured` target and its op-sequence kin take a
# serialized value instead, which a run does not produce. Seed those by driving
# their mutator library, as `docs/contributing/howto/testing/fuzzing.md` describes.
#
# Each `-raw` target is paired with the un-split name the older lines still use,
# where the payload is still one `Arbitrary` variant behind a selector byte. A
# name no line has resolves to no corpus dir and is skipped.
TARGETS: dict[str, tuple[Target, ...]] = {
	'calldata': (
		Target('executor/crates/calldata', 'genvm-common-decode'),
		Target('executor', 'entry-data-raw'),
		Target('executor', 'entry-data', _RAW),
	),
	'code': (
		Target('executor', 'runners-parse-raw'),
		Target('executor', 'runners-parse', _RAW),
	),
	'leader-result': (
		Target('executor', 'leader-result-raw'),
		Target('executor', 'leader-result', _RAW),
	),
}

_LINE_DIR = re.compile(r'v(\d+\.\d+)\.x')
# Fed to `afl-showmap`, matching what the fuzz cases use.
_EXEC_TIMEOUT_MS = '5000'


@dataclasses.dataclass(frozen=True)
class Blob:
	"""One recovered payload: `kind` says which format it is in."""

	line: str
	kind: str
	data: bytes
	origin: str


@dataclasses.dataclass
class Corpus:
	"""The seeds one target gained, against what its corpus already holds."""

	line: str
	target: Target
	inputs: Path
	known: set[str]
	seeds: dict[str, bytes] = dataclasses.field(default_factory=dict)


def configure(parser):
	parser.add_argument(
		'--artifacts-dir',
		help='run to harvest, defaults to the configured artifacts dir',
	)
	parser.add_argument(
		'--max-size',
		type=int,
		default=64 * 1024,
		help='drop a seed above this many bytes (default: 65536)',
	)
	parser.add_argument(
		'-n',
		'--dry-run',
		action='store_true',
		help='report what would be added without writing it',
	)
	parser.add_argument(
		'--no-verify',
		action='store_true',
		help='keep every new seed, without checking with afl-showmap that it '
		'reaches code the corpus does not',
	)


def main(ctx: common.Context, args) -> int:
	config = common.monorepo_config(ctx.root)
	if args.artifacts_dir:
		artifacts = Path(args.artifacts_dir)
	else:
		artifacts = ctx.root / config.get('artifacts_dir', 'build/test-artifacts')
	cases = artifacts / 'cases'
	if not cases.is_dir():
		raise common.ToolError(f'no case artifacts under {cases}: run the suite first')

	# The blobs are recovered with the repo's own encoders, which live on the
	# test runner's import path rather than in this package.
	common.add_extra_python_paths(ctx.root)

	plan = _plan(ctx, args, _blobs(ctx, cases))
	verify = not args.no_verify and _have_showmap(ctx)

	total = 0
	for corpus in sorted(plan.values(), key=lambda c: c.inputs):
		seeds = _verified(ctx, corpus) if verify else corpus.seeds
		if not seeds:
			continue
		if not args.dry_run:
			for name, data in seeds.items():
				path = misc.fuzz_input_path(corpus.inputs, name)
				path.parent.mkdir(parents=True, exist_ok=True)
				path.write_bytes(data)
		total += len(seeds)
		ctx.printer.put(
			'seeded',
			corpus=str(corpus.inputs.relative_to(ctx.root)),
			added=len(seeds),
			had=len(corpus.known),
		)

	ctx.printer.put('fuzz-corpus', added=total, dry_run=bool(args.dry_run))
	return 0


# --- harvesting -------------------------------------------------------------


def _case_line(path: Path, cases: Path) -> str | None:
	"""The executor line an artifact belongs to, or None when it names none."""
	parts = path.relative_to(cases).parts
	if len(parts) < 2 or parts[0] != 'executors':
		return None
	match = _LINE_DIR.fullmatch(parts[1])
	return match.group(1) if match else None


def _blobs(ctx: common.Context, cases: Path) -> Iterator[Blob]:
	"""Every payload a finished run left behind, in no particular order."""
	import genvm_tool_plugins.runners as runners
	from gvm_extra.case_inputs import encode_calldata

	unresolved = ''
	for config_path in sorted(cases.rglob('config.json')):
		line = _case_line(config_path, cases)
		if line is None:
			continue
		conf = json.loads(config_path.read_text())
		if 'calldata' not in conf:  # not an integration case
			continue

		try:
			calldata = encode_calldata(conf)
		except Exception as exc:
			print(f'{config_path}: calldata does not evaluate: {exc}')
		else:
			yield Blob(line, 'calldata', calldata, str(config_path))

		code = _code_path(conf, config_path.parent)
		if code is None:
			continue
		data = code.read_bytes()
		try:
			data = runners.substitute(data, ctx.root)
		except (KeyError, FileNotFoundError) as exc:
			# A uid token only differs from the built uid by its bytes, so the
			# seed is still worth keeping; the run itself would have failed.
			unresolved = str(exc)
		yield Blob(line, 'code', data, str(code))

	if unresolved:
		ctx.logger.warning('kept runner uid tokens verbatim', reason=unresolved)

	for pickle_path in sorted(cases.rglob('*_leader_nondet.pickle')):
		line = _case_line(pickle_path, cases)
		if line is None:
			continue
		# Written by the leader step of the same suite, as `code || payload`
		# frames -- which is what a validator's parser is handed.
		for index, result in enumerate(pickle.loads(pickle_path.read_bytes())):
			yield Blob(line, 'leader-result', bytes(result), f'{pickle_path}#{index}')


def _code_path(conf: dict, case_dir: Path) -> Path | None:
	"""The contract code as the executor saw it, or None if the case ships none."""
	code = conf.get('code')
	if code is None:
		return None
	path = Path(code)
	if path.suffix == '.wat':
		# `.wat` is a source the run compiles next to the config; the parser only
		# ever sees the module.
		path = case_dir / path.with_suffix('.wasm').name
	return path if path.is_file() else None


# --- planning ---------------------------------------------------------------


def _plan(ctx: common.Context, args, blobs: Iterator[Blob]) -> dict[Path, Corpus]:
	"""Group the seeds worth adding by corpus, printing every rejection."""
	plan: dict[Path, Corpus] = {}
	for blob in blobs:
		for target in TARGETS[blob.kind]:
			inputs = _inputs_dir(ctx.root, blob.line, target)
			if inputs is None:  # the line has no such target
				continue

			data = target.prefix + blob.data
			if len(data) > args.max_size:
				print(f'{blob.origin}: {len(data)} bytes, over --max-size')
				continue
			host_path = _host_path(ctx.root, data)
			if host_path is not None:
				print(f'{blob.origin}: embeds a host path ({host_path})')
				continue

			corpus = plan.get(inputs)
			if corpus is None:
				known = _known(inputs) | {
					misc.fuzz_input_name(data) for data in _curated(inputs).values()
				}
				corpus = Corpus(line=blob.line, target=target, inputs=inputs, known=known)
				plan[inputs] = corpus

			name = misc.fuzz_input_name(data)
			if name not in corpus.known and name not in corpus.seeds:
				corpus.seeds[name] = data
	return plan


def _known(inputs: Path) -> set[str]:
	return {entry.name for entry in misc.fuzz_corpus_entries(inputs)}


def _curated(inputs: Path) -> dict[str, bytes]:
	"""
	The hand-written seeds beside a corpus, by their free-form file names.

	They are fed to the fuzzer as seeds and never rewritten, so a harvested blob
	one of them already holds is not worth adding.
	"""
	curated = inputs.parent / f'{inputs.name}-curated'
	if not curated.is_dir():
		return {}
	return {
		entry.name: entry.read_bytes()
		for entry in sorted(curated.iterdir())
		if entry.is_file() and not entry.name.startswith('.')
	}


def _inputs_dir(root: Path, line: str, target: Target) -> Path | None:
	"""A line's corpus dir for `target`, or None when that line lacks the target."""
	fuzz = root / 'executors' / f'v{line}.x' / target.crate / 'fuzz'
	if not fuzz.joinpath(f'{target.name}.rs').is_file():
		return None
	return fuzz / f'inputs-{target.name}'


def _host_path(root: Path, data: bytes) -> str | None:
	"""
	The first path of the harvesting machine `data` carries, if any.

	Corpus entries are committed, and a zip or a wasm built here embeds the
	directory it was built in; that turns a seed into a record of somebody's
	home dir.
	"""
	for candidate in (str(root), str(Path.home()), '/nix/store'):
		if candidate.encode() in data:
			return candidate
	return None


# --- verification -----------------------------------------------------------


def _have_showmap(ctx: common.Context) -> bool:
	if shutil.which('cargo-afl'):
		return True
	ctx.logger.warning('cargo-afl not found, keeping every seed unverified')
	return False


def _verified(ctx: common.Context, corpus: Corpus) -> dict[str, bytes]:
	"""
	The seeds that reach an edge the corpus does not already cover.

	A harvested blob can be rejected by the target for a reason of its own --
	the wrong line's format, a shape the parser drops at the first byte -- and
	then it only makes the committed corpus bigger.
	"""
	binary = _fuzz_binary(ctx.root, corpus.line, corpus.target.name)
	if binary is None:
		ctx.logger.warning(
			'fuzz target is not built, keeping its seeds unverified',
			target=corpus.target.name,
			line=corpus.line,
		)
		return corpus.seeds

	with tempfile.TemporaryDirectory() as tmp_name:
		tmp = Path(tmp_name)
		staged = tmp / 'seeds'
		staged.mkdir()
		for name, data in corpus.seeds.items():
			staged.joinpath(name).write_bytes(data)

		# What the committed seeds already reach: a harvested blob is only worth
		# adding on top of both dirs, the curated one included
		baseline = tmp / 'baseline'
		baseline.mkdir()
		for name, data in _curated(corpus.inputs).items():
			# Curated names are free-form, so they are not guaranteed to differ
			# from the corpus' content hashes
			baseline.joinpath(f'curated-{name}').write_bytes(data)
		# Flattened: `showmap` keys its output by file name, which is unique anyway
		for entry in misc.fuzz_corpus_entries(corpus.inputs):
			shutil.copyfile(entry, baseline / entry.name)

		covered: set[int] = set()
		if any(baseline.iterdir()):
			for edges in _showmap(binary, baseline, tmp / 'corpus').values():
				covered |= edges
		reached = _showmap(binary, staged, tmp / 'new')

	kept: dict[str, bytes] = {}
	# Smallest first, so a seed is judged against the cheapest cover of its edges.
	for name, data in sorted(corpus.seeds.items(), key=lambda item: len(item[1])):
		edges = reached.get(name)
		if not edges:
			print(f'{corpus.inputs}/{name}: the target did not run it')
			continue
		if edges <= covered:
			print(f'{corpus.inputs}/{name}: reaches nothing new')
			continue
		covered |= edges
		kept[name] = data
	return kept


def _showmap(binary: Path, inputs: Path, out: Path) -> dict[str, set[int]]:
	"""Edge ids each file in `inputs` reaches, batched through one forkserver."""
	env = dict(os.environ)
	# The targets are persistent, so a run reseeds itself from the entropy the
	# shim fakes; without it two runs of one input disagree on their edges.
	preload = binary.parent.parent / 'libgenvm_fuzz_preload.so'
	if preload.is_file():
		env['AFL_PRELOAD'] = str(preload)

	result = subprocess.run(
		[
			'cargo-afl',
			'afl',
			'showmap',
			'-i',
			str(inputs),
			'-o',
			str(out),
			'-t',
			_EXEC_TIMEOUT_MS,
			'--',
			str(binary),
		],
		env=env,
		capture_output=True,
		text=True,
	)
	if not out.is_dir():
		raise common.ToolError(
			f'afl-showmap failed for {binary.name}: {result.stderr.strip()}'
		)

	edges: dict[str, set[int]] = {}
	for path in out.iterdir():
		edges[path.name] = {
			int(line.split(':', 1)[0]) for line in path.read_text().splitlines() if line
		}
	return edges


def _fuzz_binary(root: Path, line: str, name: str) -> Path | None:
	"""Where `afl build` puts a target, as `configure` recorded the target dirs."""
	info_path = root / 'build' / 'info.json'
	if not info_path.is_file():
		return None
	info = json.loads(info_path.read_text())
	target_dir = info.get('rust_target_dirs', {}).get(f'executors/v{line}.x')
	if target_dir is None:
		return None
	binary = (
		Path(target_dir) / info['rust_target'] / 'debug' / 'examples' / f'fuzz-{name}'
	)
	return binary if binary.is_file() else None
