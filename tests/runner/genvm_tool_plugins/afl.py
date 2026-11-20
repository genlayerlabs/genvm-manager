import asyncio
import atexit
import collections.abc
import contextlib
import functools
import math
import os
import re
import shutil
import signal
import tempfile
import typing
from pathlib import Path

import genvm_tool.tests

Command = collections.abc.Sequence[str | Path]
Wrapper = collections.abc.Callable[[Command], Command]

# Secondaries outlive their step only if it never finishes — a cancelled case,
# `--fail-fast`, Ctrl-C. `run_steps` has no cleanup hook, so the fleet is bounded
# by its own `-V` and, when the tool itself goes away, by this
_live: set[asyncio.subprocess.Process] = set()


@atexit.register
def _stop_live_fuzzers() -> None:
	for process in list(_live):
		if process.returncode is None:
			with contextlib.suppress(OSError):
				process.send_signal(signal.SIGINT)
	_live.clear()


# What a fuzzing run wants and a minimization run must not have: `cmin` measures
# one input per run through `afl-showmap`, and these either steer `afl-fuzz`
# alone or (`AFL_FUZZER_LOOPCOUNT`) make the target loop instead of returning
FUZZ_ONLY_ENV = (
	'AFL_AUTORESUME',
	'AFL_BENCH_UNTIL_CRASH',
	'AFL_FUZZER_LOOPCOUNT',
	'AFL_NO_FASTRESUME',
	'AFL_TRY_AFFINITY',
)

# Ceiling for `-t`, whose auto-scaling stays below it. Chosen against the slowest
# committed seed rather than against the mean: what it has to survive is one
# entry of an existing corpus, on a core slower than the one that harvested it
EXEC_TIMEOUT_MS = 5000

# How AFL reports a seed it refused to fuzz because the target died on it
SEED_CRASH_RE = re.compile(r"Test case '([^']*)' results in a crash")


def register(ctx: genvm_tool.tests.stage.configuration.Context) -> None:
	ctx.run_parser.add_argument(
		'--fuzz-timeout',
		type=str,
		default='30s',
		help='Timeout for each fuzzing run (e.g. 30s, 5m, 1h)',
	)
	ctx.run_parser.add_argument(
		'--fuzz-jobs',
		type=int,
		default=0,
		help='Fuzzer processes per target, defaults to the cores left over by --fuzz-concurrent',
	)
	ctx.run_parser.add_argument(
		'--fuzz-concurrent',
		type=int,
		default=0,
		help='Fuzz targets to run side by side, defaults to the core count minus two. '
		'Above one there is no AFL console, only the logs',
	)
	ctx.run_parser.add_argument(
		'--fuzz-update-corpus',
		default=False,
		action='store_true',
		help='Whether to update the fuzzing corpus',
	)


def _budget() -> int:
	"""Cores this machine is willing to hand to fuzzing."""
	return max(1, (os.cpu_count() or 1) - 2)


@functools.cache
def concurrent(ctx: genvm_tool.tests.stage.collection.Context) -> int:
	"""
	How many fuzz targets may run at once.

	Different targets are worth more than instances of one: a fleet only shares
	its corpus every `AFL_SYNC_TIME`, so below that it is one search duplicated,
	while another target explores code the first one never reaches.
	"""
	# `test show` builds cases without the `run` parser, so the flags may be absent
	requested = getattr(ctx.configuration.args, 'fuzz_concurrent', 0)
	source = '--fuzz-concurrent'
	if requested <= 0:
		requested = _budget()
		source = f'os.cpu_count() - 2 = {requested}'
	ctx.shared.logger.info('Concurrent fuzz targets', count=requested, source=source)
	return requested


@functools.cache
def jobs(ctx: genvm_tool.tests.stage.collection.Context) -> int:
	requested = getattr(ctx.configuration.args, 'fuzz_jobs', 0)
	source = '--fuzz-jobs'
	if requested <= 0:
		requested = max(1, min(8, _budget() // concurrent(ctx)))
		source = f'(os.cpu_count() - 2) // {concurrent(ctx)}'
	ctx.shared.logger.info('Fuzz jobs', count=requested, source=source)
	return requested


def pool_usage(ctx: genvm_tool.tests.stage.collection.Context) -> dict[str, typing.Any]:
	"""
	How a fuzz case takes the case pool.

	A lone target owns the console and therefore the whole pool; several of them
	share it, each holding as many permits as it starts fuzzer processes.
	"""
	solo = concurrent(ctx) == 1
	return {'console_pool': solo, 'permits': 1 if solo else jobs(ctx)}


def timeout(ctx: genvm_tool.tests.stage.collection.Context) -> int:
	value = str(getattr(ctx.configuration.args, 'fuzz_timeout', '30s'))
	return max(1, math.ceil(genvm_tool.misc.parse_duration(value)))


def environment(base: dict[str, str]) -> dict[str, str]:
	"""``base`` plus the AFL knobs every target shares."""
	env = dict(base)
	env.update(
		{
			# The output dir is reused so a rerun continues the search; without
			# this AFL refuses to start on it. NB it then reads its own queue and
			# ignores `-i`, so a corpus update only takes effect after the output
			# dir is removed
			'AFL_AUTORESUME': '1',
			# Stop *successfully* on the first saved crash, so a finding is
			# reported by `fleet_steps` rather than by the exit code
			'AFL_BENCH_UNTIL_CRASH': '1',
			'AFL_FUZZER_LOOPCOUNT': '20',  # without it no coverage will be written!
			'AFL_NO_CFG_FUZZING': '1',
			# The fast-resume cache was observed to fail the next run with `Bad
			# file descriptor` after a fleet was interrupted
			'AFL_NO_FASTRESUME': '1',
			# Persistent mode carries state across iterations, so calibration
			# reports most of the corpus as unstable; the warning is pure noise
			'AFL_NO_WARN_INSTABILITY': '1',
			# A fleet wants a core each, and AFL aborts rather than share one; on
			# a busy machine an unbound instance is still better than a failed case
			'AFL_TRY_AFFINITY': '1',
		}
	)
	return env


def crashing_seeds(out_dir: Path) -> list[str]:
	"""
	Committed inputs an instance found to crash the target while starting up.

	`-t` makes AFL skip a problematic seed instead of aborting, which is what a
	corpus that only grows needs for a slow entry — but a crashing one is a
	finding, and skipped it would be reported nowhere. Read back out of the logs
	rather than prevented, since the two share the one switch.
	"""
	found: list[str] = []
	for log in sorted(out_dir.glob('*.log')):
		for match in SEED_CRASH_RE.finditer(log.read_text(errors='replace')):
			name = match.group(1)
			_, _, original = name.rpartition('orig:')
			if (original or name) not in found:
				found.append(original or name)
	return found


def output_dir(
	shared: genvm_tool.tests.SharedContext, project_dir: Path, name: str
) -> Path:
	"""
	Where one target's AFL output lives.

	Keyed by project, not by target name alone: the executor lines share fuzz
	target stems, and one output dir per stem would mix their corpora — which
	`cmin` then writes back into the committed inputs.
	"""
	return shared.artifacts_dir / 'fuzz' / project_dir.relative_to(shared.root_dir) / name


def current_umask() -> int:
	mask = os.umask(0)
	os.umask(mask)
	return mask


def saved_findings(path: Path) -> list[Path]:
	"""Inputs AFL saved in one of its finding dirs, minus the README it drops there."""
	if not path.is_dir():
		return []
	return sorted(
		entry
		for entry in path.iterdir()
		if entry.is_file() and entry.name != 'README.txt' and not entry.name.startswith('.')
	)


def curated_dir(inputs_dir: Path) -> Path:
	"""
	Where a target's hand-written seeds live, beside its generated corpus.

	Not in it: a corpus update replaces that dir with a `cmin` of the queue, and a
	seed someone wrote to pin a format down has to survive the run that decides it
	is redundant. Names here are free-form, they are for humans to read.
	"""
	return inputs_dir.parent / f'{inputs_dir.name}-curated'


def curated_inputs(inputs_dir: Path) -> dict[str, bytes]:
	"""Curated seeds of a target, keyed by the name the corpus would give them."""
	result: dict[str, bytes] = {}
	for entry in saved_findings(curated_dir(inputs_dir)):
		data = entry.read_bytes()
		result[genvm_tool.misc.fuzz_input_name(data)] = data
	return result


def add_to_corpus(
	inputs_dir: Path, findings: collections.abc.Iterable[Path]
) -> list[str]:
	"""
	Commit findings as generated corpus entries, under the names a corpus uses.

	A crash is only replayed by a later run if some dir handed to `-i` holds it,
	and the output dir it was saved in is wiped by the next run's crash reset.
	"""
	known = set(curated_inputs(inputs_dir)) | {
		entry.name for entry in genvm_tool.misc.fuzz_corpus_entries(inputs_dir)
	}
	added: list[str] = []
	for entry in findings:
		data = entry.read_bytes()
		name = genvm_tool.misc.fuzz_input_name(data)
		if name in known:
			continue
		path = genvm_tool.misc.fuzz_input_path(inputs_dir, name)
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(data)
		known.add(name)
		added.append(name)
	return added


def fleet_steps(
	ctx: genvm_tool.tests.stage.collection.Context,
	*,
	cwd: Path,
	env: dict[str, str],
	inputs_dir: Path,
	out_dir: Path,
	launcher: Command,
	target: Command,
	wrap: Wrapper = lambda argv: argv,
	prepare_command: Command | None = None,
	status_command: Command | None = None,
	replay: str | None = None,
) -> list[genvm_tool.tests.exec.step.Step]:
	"""
	Steps running one AFL fleet over ``target`` and reporting what it saved.

	One `-M` plus N-1 `-S` instances share ``out_dir`` and sync their corpora
	through it, so the whole fleet is one case with one verdict. ``wrap`` gets
	the last word on the argv, for a launcher that has to be reached through a
	shell (the Python env, say).
	"""
	instances = ['main'] + [f'sec{i}' for i in range(1, jobs(ctx))]
	secondaries: list[tuple[asyncio.subprocess.Process, typing.IO[bytes]]] = []
	watchdog_tokens: dict[int, int] = {}
	# AFL takes one `-i`, so the two committed dirs are staged into one
	seeds_dir = out_dir.parent / f'{out_dir.name}-seeds'
	# Targets sharing the terminal cannot each draw a full-screen UI, so with more
	# than one in flight nobody does and `main` is read from its log like the rest
	solo = concurrent(ctx) == 1
	env = dict(env) if solo else {**env, 'AFL_NO_UI': '1'}

	def command(role: str, instance: str) -> Command:
		return wrap(
			[
				*launcher,
				# CmpLog needs a second instrumented binary and buys little on
				# these targets, see the fuzzing explanation page
				'-c',
				'-',
				# A ceiling on the auto-scaled per-exec timeout. AFL's own default
				# is 1000ms, and a corpus entry above it aborts the whole run
				# rather than being skipped; python targets run traced and a slow
				# CI core is enough to cross it
				'-t',
				f'{EXEC_TIMEOUT_MS}+',
				role,
				instance,
				'-i',
				seeds_dir,
				'-o',
				out_dir,
				'-V',
				str(timeout(ctx)),
				*target,
			]
		)

	def crash_dirs() -> list[Path]:
		# Globbed, not derived from `instances`: a run with fewer jobs than the
		# last one still has to account for what the wider fleet left behind
		return sorted(out_dir.glob('*/crashes'))

	async def stage_seeds(_) -> genvm_tool.tests.test.Result | None:
		# Rebuilt every run: a curated seed added since the last one has to reach
		# `-i`, and one removed from either dir must not linger
		shutil.rmtree(seeds_dir, ignore_errors=True)
		seeds_dir.mkdir(parents=True)
		# Flattened: the corpus is sharded by name, and AFL is happier with one dir
		for entry in genvm_tool.misc.fuzz_corpus_entries(inputs_dir):
			shutil.copyfile(entry, seeds_dir / entry.name)
		for name, data in curated_inputs(inputs_dir).items():
			seeds_dir.joinpath(name).write_bytes(data)

		staged = saved_findings(seeds_dir)
		# A result either way: this is the first step, so the error check after it
		# has nothing else to read
		return genvm_tool.tests.test.Result(
			passed=bool(staged),
			context={'error': f'no seeds in {inputs_dir} or {curated_dir(inputs_dir)}'}
			if not staged
			else {'seeds': len(staged)},
			elapsed_seconds=0,
		)

	async def drop_previous_crashes(_):
		# The verdict below describes this run. AFL keeps what earlier ones saved,
		# and the output dir is deliberately reused so fuzzing resumes rather than
		# restarts
		for path in crash_dirs():
			shutil.rmtree(path, ignore_errors=True)

	async def stop(process: asyncio.subprocess.Process, log: typing.IO[bytes]) -> None:
		try:
			if process.returncode is None:
				# AFL writes its queue out on SIGINT, so the next run resumes
				process.send_signal(signal.SIGINT)
			else:
				# It was gone before the fleet was stopped, so it never fuzzed for
				# its whole `-V`; the case still passes on `main` alone
				ctx.shared.logger.warning(
					'a fuzz secondary exited early',
					returncode=process.returncode,
					log=str(getattr(log, 'name', '')),
				)
			try:
				await asyncio.wait_for(process.wait(), timeout=30)
			except TimeoutError:
				process.kill()
				await process.wait()
		finally:
			log.close()
			_live.discard(process)
			token = watchdog_tokens.pop(process.pid, None)
			if token is not None:
				ctx.shared.watchdog.unregister(token)

	async def start_secondaries(_):
		# The console, when there is one, belongs to `main`; the others write to a
		# log next to their queue instead. They exit on their own `-V`; the stop
		# step is for the run that ends early, on a crash or a failure of `main`
		try:
			for instance in instances[1:]:
				log = (out_dir / f'{instance}.log').open('wb')
				try:
					process = await asyncio.create_subprocess_exec(
						*command('-S', instance),
						cwd=cwd,
						env=env,
						stdin=asyncio.subprocess.DEVNULL,
						stdout=log,
						stderr=asyncio.subprocess.STDOUT,
					)
				except BaseException:
					log.close()
					raise
				secondaries.append((process, log))
				_live.add(process)
				# `_live` only covers an orderly exit; a fleet outliving a killed
				# tool is what the watchdog is for
				watchdog_tokens[process.pid] = ctx.shared.watchdog.register(
					genvm_tool.tests.exec.command.kill_process_cleanup(process.pid)
				)
		except BaseException:
			# A half-started fleet would otherwise hold its cores and its output
			# dir for the rest of the suite
			for process, log in secondaries:
				await stop(process, log)
			secondaries.clear()
			raise

	async def stop_secondaries(previous_results: list[typing.Any]) -> typing.Any:
		for process, log in secondaries:
			try:
				await stop(process, log)
			except Exception as error:
				# One instance that will not die must not strand the others
				ctx.shared.logger.warning('could not stop a fuzz secondary', error=error)
		secondaries.clear()
		# `CommandToResultStep` reads the step before it, which must stay the
		# command result of `main`
		return previous_results[-1]

	async def save_main_log(previous_results: list[typing.Any]) -> typing.Any:
		result = previous_results[-1]
		assert isinstance(result, genvm_tool.tests.exec.command.Result)
		# Without the console `main`'s output is only in the case log, next to
		# everything else; the fleet's own logs are what one reads afterwards
		(out_dir / 'main.log').write_text(result.stdout + result.stderr)
		return result

	async def report_findings(
		previous_results: list[typing.Any],
	) -> genvm_tool.tests.test.Result:
		result = previous_results[-1]
		assert isinstance(result, genvm_tool.tests.test.Result)

		crashes = [crash for path in crash_dirs() for crash in saved_findings(path)]
		context = dict(result.context)
		bad_seeds = crashing_seeds(out_dir)
		if bad_seeds:
			context['crashing_seeds'] = bad_seeds
		if crashes:
			context['crashes'] = [str(crash) for crash in crashes]
			if replay is not None:
				context['replay'] = replay
			added = add_to_corpus(inputs_dir, crashes)
			if added:
				context['corpus'] = f'{len(added)} crash(es) added to {inputs_dir}'
		if status_command is not None and len(instances) > 1:
			# `-M`'s console shows that instance alone, so the fleet is invisible
			# without asking for the aggregate
			try:
				process = await asyncio.create_subprocess_exec(
					*status_command,
					cwd=cwd,
					env=env,
					stdout=asyncio.subprocess.PIPE,
					stderr=asyncio.subprocess.DEVNULL,
				)
				summary, _ = await process.communicate()
				context['fleet'] = (
					summary.decode(errors='replace')
					if process.returncode == 0
					else f'{status_command[0]} exited with {process.returncode}'
				)
			except OSError as error:
				# A status tool that cannot run must not turn a clean run into a
				# failure
				context['fleet'] = f'{status_command[0]}: {error}'

		return genvm_tool.tests.test.Result(
			passed=result.passed and not crashes and not bad_seeds,
			context=context,
			elapsed_seconds=result.elapsed_seconds,
		)

	return [
		genvm_tool.tests.exec.step.SetCwd(path=cwd),
		*[genvm_tool.tests.exec.step.SetEnv(key=k, value=v) for k, v in env.items()],
		genvm_tool.tests.exec.step.MkDir(path=out_dir),
		genvm_tool.tests.exec.step.PythonFunction(stage_seeds),
		genvm_tool.tests.test.ResultStopIfErrorStep(),
		genvm_tool.tests.exec.step.PythonFunction(drop_previous_crashes),
		*(
			[]
			if prepare_command is None
			else [
				genvm_tool.tests.exec.step.Run(
					args=prepare_command,
					mode=genvm_tool.tests.exec.command.RunMode.INTERACTIVE,
				),
				genvm_tool.tests.test.CommandToResultStep(),
				genvm_tool.tests.test.ResultStopIfErrorStep(),
			]
		),
		genvm_tool.tests.exec.step.PythonFunction(start_secondaries),
		genvm_tool.tests.exec.step.Run(
			# A lone instance runs as `-S`: `-M` would add the deterministic
			# stage, which a short budget cannot afford
			args=command('-M' if len(instances) > 1 else '-S', 'main'),
			mode=genvm_tool.tests.exec.command.RunMode.INTERACTIVE_TTY
			if solo
			else genvm_tool.tests.exec.command.RunMode.SILENT,
		),
		*([] if solo else [genvm_tool.tests.exec.step.PythonFunction(save_main_log)]),
		genvm_tool.tests.exec.step.PythonFunction(stop_secondaries),
		genvm_tool.tests.test.CommandToResultStep(),
		# Findings are reported before the abort check, so a run that crashed and
		# then failed still names the crash
		genvm_tool.tests.exec.step.PythonFunction(report_findings),
		genvm_tool.tests.test.ResultStopIfErrorStep(),
	]


def corpus_update_steps(
	ctx: genvm_tool.tests.stage.collection.Context,
	*,
	inputs_dir: Path,
	out_dir: Path,
	cmin_command: collections.abc.Callable[[Path, Path], Command],
	unset_env: collections.abc.Sequence[str] = (),
) -> list[genvm_tool.tests.exec.step.Step]:
	"""
	Steps replacing the committed corpus with a `cmin` of what the fleet found.

	``cmin_command`` is given the input and the output dir, in that order. These
	steps go after `fleet_steps`, whose cwd and environment they reuse.
	"""
	if not getattr(ctx.configuration.args, 'fuzz_update_corpus', False):
		return []

	opt_dir = out_dir.parent / f'{out_dir.name}-opt'
	# `cmin` walks its input dir recursively and skips only dotfiles, so it would
	# otherwise mistake crashes and instance logs for corpus entries
	queue_dir = out_dir.parent / f'{out_dir.name}-queue'

	async def prepare(_) -> genvm_tool.tests.test.Result | None:
		for path in (opt_dir, queue_dir):
			shutil.rmtree(path, ignore_errors=True)
			path.mkdir(parents=True)

		for entry in sorted(out_dir.glob('*/queue/*')):
			if not entry.is_file() or entry.name.startswith('.'):
				continue
			data = entry.read_bytes()
			queue_dir.joinpath(genvm_tool.misc.fuzz_input_name(data)).write_bytes(data)

		if not any(queue_dir.iterdir()):
			return genvm_tool.tests.test.Result(
				passed=False,
				context={'error': f'no queue entries under {out_dir}'},
				elapsed_seconds=0,
			)
		return None

	async def replace(_) -> genvm_tool.tests.test.Result:
		minimized = saved_findings(opt_dir)
		# A curated seed is fed to every run, so `cmin` keeps it whenever it is
		# worth keeping; written back it would be committed twice
		curated = set(curated_inputs(inputs_dir))
		kept: dict[str, bytes] = {}
		for entry in minimized:
			data = entry.read_bytes()
			name = genvm_tool.misc.fuzz_input_name(data)
			if name not in curated:
				kept[name] = data

		if not minimized:
			# The old corpus is the only copy of the seeds someone wrote by hand,
			# so an empty minimization is a failure, not a reason to delete it
			return genvm_tool.tests.test.Result(
				passed=False,
				context={'error': f'cmin produced no inputs in {opt_dir}'},
				elapsed_seconds=0,
			)

		inputs_dir.parent.mkdir(parents=True, exist_ok=True)
		staged = Path(
			tempfile.mkdtemp(prefix=f'.{inputs_dir.name}.new-', dir=inputs_dir.parent)
		)
		# `mkdtemp` is 0700, and this directory ends up as the committed corpus
		staged.chmod(0o777 & ~current_umask())
		backup: Path | None = None
		try:
			for name, data in kept.items():
				path = genvm_tool.misc.fuzz_input_path(staged, name)
				path.parent.mkdir(parents=True, exist_ok=True)
				path.write_bytes(data)
			if inputs_dir.exists():
				backup = Path(
					tempfile.mkdtemp(prefix=f'.{inputs_dir.name}.old-', dir=inputs_dir.parent)
				)
				backup.rmdir()
				inputs_dir.rename(backup)
			try:
				staged.rename(inputs_dir)
			except BaseException as error:
				if backup is not None:
					try:
						backup.rename(inputs_dir)
					except OSError:
						raise RuntimeError(
							f'corpus swap failed and the old corpus is left in {backup}'
						) from error
				raise
			if backup is not None:
				shutil.rmtree(backup, ignore_errors=True)
		finally:
			shutil.rmtree(staged, ignore_errors=True)

		context = {'corpus': f'{len(kept)} inputs written to {inputs_dir}'}
		if len(kept) != len(minimized):
			context['curated'] = (
				f'{len(minimized) - len(kept)} kept by {curated_dir(inputs_dir)}'
			)
		return genvm_tool.tests.test.Result(passed=True, context=context, elapsed_seconds=0)

	return [
		genvm_tool.tests.exec.step.PythonFunction(prepare),
		genvm_tool.tests.test.ResultStopIfErrorStep(),
		*[genvm_tool.tests.exec.step.UnsetEnv(key=key) for key in FUZZ_ONLY_ENV],
		*[genvm_tool.tests.exec.step.UnsetEnv(key=key) for key in unset_env],
		genvm_tool.tests.exec.step.Run(
			args=cmin_command(queue_dir, opt_dir),
			mode=genvm_tool.tests.exec.command.RunMode.INTERACTIVE,
		),
		genvm_tool.tests.test.CommandToResultStep(),
		genvm_tool.tests.test.ResultStopIfErrorStep(),
		genvm_tool.tests.exec.step.PythonFunction(replace),
		genvm_tool.tests.test.ResultStopIfErrorStep(),
	]
