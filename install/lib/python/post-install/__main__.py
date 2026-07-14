import argparse
import hashlib
import io
import json
import logging
import os
import platform
import shlex
import shutil
import subprocess
import tarfile
import traceback
import typing
from pathlib import Path

target_os = platform.system().lower()
if target_os == 'darwin':
	target_os = 'macos'
if target_os not in ('linux', 'macos'):
	target_os = 'linux'  # default to linux


target_arch = platform.machine()
target_arch = {
	'x86_64': 'amd64',
	'aarch64': 'arm64',
	'arm64': 'arm64',
	'amd64': 'amd64',
}.get(target_arch, 'amd64')


def str_to_bool(value):
	if value.lower() in ('yes', 'true', 't', 'y', '1'):
		return True
	elif value.lower() in ('no', 'false', 'f', 'n', '0'):
		return False
	else:
		raise argparse.ArgumentTypeError('Boolean value expected.')


def str_to_bool_or_none(value):
	if value is None or value.lower() in ('none', 'default', 'null', 'nil', ''):
		return None
	return str_to_bool(value)


parser = argparse.ArgumentParser()
parser.add_argument(
	'--os',
	type=str,
	default=target_os,
	help='Target operating system (linux/macos)',
)
parser.add_argument(
	'--arch',
	type=str,
	default=target_arch,
	help='Target architecture (amd64/arm64)',
)
parser.add_argument(
	'--error-on-missing-executor',
	type=str_to_bool,
	default=True,
	help='Whether to error on missing executor',
)
parser.add_argument(
	'--use-patchelf',
	type=str_to_bool,
	default=False,
	help='Set the ELF interpreter with patchelf instead of lief',
)
parser.add_argument(
	'--log-level',
	type=str,
	default='INFO',
	help='Logging level',
	choices=[
		'DEBUG',
		'INFO',
		'WARNING',
		'ERROR',
		'CRITICAL',
	],
)

step_names = [
	'executor-download',
	'runners-download',
	'bin-patch',
	'bin-check',
	'precompile',
]

parser.add_argument(
	'--default-steps',
	type=str_to_bool,
	default=True,
	help='Default value for steps when not explicitly set',
)
parser.add_argument(
	'--default-download',
	type=str_to_bool_or_none,
	default=None,
	help='Default value for download steps (default: use --default-steps)',
)
parser.add_argument(
	'--executor-download',
	type=str_to_bool_or_none,
	default=None,
	help='Enable/disable executor download step (default: use --default-download)',
)
parser.add_argument(
	'--runners-download',
	type=str_to_bool_or_none,
	default=None,
	help='Enable/disable runners download step (default: use --default-download)',
)
parser.add_argument(
	'--bin-patch',
	type=str_to_bool_or_none,
	default=None,
	help='Enable/disable bin patch step (default: use --default-steps)',
)
parser.add_argument(
	'--bin-check',
	type=str_to_bool_or_none,
	default=None,
	help='Enable/disable bin check step (default: use --default-steps)',
)
parser.add_argument(
	'--precompile',
	type=str_to_bool_or_none,
	default=None,
	help='Enable/disable precompile step (default: use --default-steps)',
)

args = parser.parse_args()

# Apply default to None values
if args.default_download is None:
	args.default_download = args.default_steps

if args.executor_download is None:
	args.executor_download = args.default_download
if args.runners_download is None:
	args.runners_download = args.default_download
if args.bin_patch is None:
	args.bin_patch = args.default_steps
if args.bin_check is None:
	args.bin_check = args.default_steps
if args.precompile is None:
	args.precompile = args.default_steps

log_level_str = args.log_level
log_levels = {
	'DEBUG': logging.DEBUG,
	'INFO': logging.INFO,
	'WARNING': logging.WARNING,
	'ERROR': logging.ERROR,
	'CRITICAL': logging.CRITICAL,
}

logging.basicConfig(level=log_levels.get(log_level_str, logging.INFO))

logger = logging.getLogger(__name__)

logging.info('Starting actual post-install script')

# Interpreter patching rewrites the ELF interpreter (dynamic loader) to the
# bundled one, whose path is only known at install time. It applies to ELF
# binaries (Linux) only; on other targets the loader is fixed and rpath/needed
# entries are already set correctly at build time, so nothing is needed.
patch_interpreter = args.bin_patch and args.os == 'linux'

# `--use-patchelf` shells out to patchelf instead, so lief — a heavy binary
# wheel pulled in for this one job — is never imported (nor installed into the
# venv; see the wrapper).
if patch_interpreter and not args.use_patchelf:
	import lief

	lief.logging.set_level(lief.logging.LEVEL.ERROR)

# gvm32 (Crockford's Base32, big-endian, no padding) — the encoding the executor
# uses for runner hashes / on-disk paths. MUST match support/runner-script.py,
# executor/crates/sdk-rs/src/gvm32.rs and runners/genlayer-py-std/.../gvm32.py.
# NOT Nix base32 (which uses a different alphabet AND little-endian bit order).
HASH_VALID_CHARS = '0123456789abcdefghjkmnpqrstvwxyz'


def digest_to_hash_id(got_hash: bytes) -> str:
	out = []
	value = 0
	bits = 0
	for byte in got_hash:
		value = (value << 8) | byte
		bits += 8
		while bits >= 5:
			bits -= 5
			out.append(HASH_VALID_CHARS[(value >> bits) & 0x1F])
	if bits > 0:
		out.append(HASH_VALID_CHARS[(value << (5 - bits)) & 0x1F])
	return ''.join(out)


def runner_check_bytes(data: bytes, hash: str) -> bool:
	digest = hashlib.sha256(data).digest()
	my_hash = digest_to_hash_id(digest)
	return my_hash == hash


genvm_root_dir = Path(__file__).parent.parent.parent.parent
logger.info(f'Executor root directory: {genvm_root_dir}')

_interpreter_path: Path | None = None


def get_interpreter_path():
	global _interpreter_path
	if _interpreter_path is not None:
		return _interpreter_path
	interpreter_path = genvm_root_dir.joinpath('lib', 'libc.so').absolute()
	logger.info(f'Interpreter path: {interpreter_path}')

	if not interpreter_path.exists():
		logger.error(
			f'Interpreter path {interpreter_path} does not exist, cannot patch executables'
		)
		exit(1)
	_interpreter_path = interpreter_path
	return interpreter_path


def _patch_executable_lief(path: Path):
	binary = lief.parse(path)
	if not binary:
		logger.error(f'Failed to parse binary at {path}')
		return

	# rpath/needed entries are set correctly at build time, so the only install
	# time fixup left is the ELF interpreter (dynamic loader) path.
	if binary.format != lief.Binary.FORMATS.ELF:
		logger.info(f'{path} is not ELF ({binary.format}), nothing to patch')
		return

	logger.info(f'Current interpreter: {binary.interpreter}')
	if Path(binary.interpreter).exists():
		logger.info(
			f'Interpreter {binary.interpreter} exists, skipping interpreter patching'
		)
		return

	old_interpreter = binary.interpreter
	binary.interpreter = str(get_interpreter_path())
	logger.info(f'Updated interpreter from {old_interpreter} to: {binary.interpreter}')

	binary.write(str(path))
	logger.info(f'Successfully patched interpreter: {path}')


def _patch_executable_patchelf(path: Path):
	patchelf = shutil.which('patchelf')
	if patchelf is None:
		raise RuntimeError('--use-patchelf was given but patchelf is not on PATH')

	if detect_executable_platform(path) != 'linux':
		logger.info(f'{path} is not ELF, nothing to patch')
		return

	current = subprocess.run(
		[patchelf, '--print-interpreter', str(path)],
		capture_output=True,
		text=True,
	)
	if current.returncode == 0:
		current_interpreter = current.stdout.strip()
		logger.info(f'Current interpreter: {current_interpreter}')
		if current_interpreter and Path(current_interpreter).exists():
			logger.info(
				f'Interpreter {current_interpreter} exists, skipping interpreter patching'
			)
			return
	else:
		# A static binary has no interpreter to print; there is nothing to patch.
		logger.info(f'{path} has no interpreter, nothing to patch')
		return

	interpreter = get_interpreter_path()
	subprocess.run(
		[patchelf, '--set-interpreter', str(interpreter), str(path)],
		check=True,
		text=True,
	)
	logger.info(f'Successfully patched interpreter: {path} -> {interpreter}')


def patch_executable(path: Path):
	logger.info(f'Patching interpreter for {path}')
	if not path.exists():
		logger.warning(f'Path {path} does not exist, skipping patching')
		return

	if args.use_patchelf:
		_patch_executable_patchelf(path)
	else:
		_patch_executable_lief(path)


# linux ships ELF, macos ships Mach-O; these are the only OSes we target.
BinaryOS = typing.Literal['linux', 'macos']

# Mach-O magics (thin 32/64-bit and universal/fat), in both stored byte orders.
_MACHO_MAGICS = frozenset(
	{
		b'\xfe\xed\xfa\xce',
		b'\xce\xfa\xed\xfe',
		b'\xfe\xed\xfa\xcf',
		b'\xcf\xfa\xed\xfe',
		b'\xca\xfe\xba\xbe',
		b'\xbe\xba\xfe\xca',
		b'\xca\xfe\xba\xbf',
		b'\xbf\xba\xfe\xca',
	}
)


def detect_executable_platform(path: Path) -> BinaryOS | None:
	"""Sniff a binary's header to identify its target OS: ELF -> linux, Mach-O
	(any variant/byte order) -> macos. Returns None for anything unrecognized
	(wrapper scripts, truncated/empty files, unknown formats)."""
	try:
		with open(path, 'rb') as f:
			magic = f.read(4)
	except OSError:
		return None
	if magic == b'\x7fELF':
		return 'linux'
	if magic in _MACHO_MAGICS:
		return 'macos'
	return None


def check_executable_platform(path: Path):
	"""Refuse a wrong-OS binary before we trust it. A bad release once shipped a
	macOS (Mach-O) executor inside a linux tarball; the missing-only download
	guard accepted it and it surfaced as an opaque `Exec format error` at
	runtime. Here we error on a clear OS mismatch instead. Unrecognized formats
	(None) are left alone rather than guessed at."""
	detected = detect_executable_platform(path)
	if detected is not None and detected != args.os:
		logger.error(
			f'{path} is a {detected} binary but target OS is {args.os}; '
			'wrong-platform executor shipped'
		)
		raise RuntimeError(f'{path} is a {detected} binary but target OS is {args.os}')


def run_check_command(command: list[str | Path]):
	env = os.environ.copy()
	env['LLVM_PROFILE_FILE'] = '/dev/null'
	logger.info(
		'>> '
		+ ' '.join([shlex.quote(x if isinstance(x, str) else str(x)) for x in command])
	)
	subprocess.run(command, check=True, text=True, env=env)


modules_executable = genvm_root_dir.joinpath('bin', 'genvm-modules')
check_executable_platform(modules_executable)
if patch_interpreter:
	patch_executable(modules_executable)

if args.bin_check:
	run_check_command([modules_executable, '--version'])


def load_micro_yaml():
	src = Path(__file__).parent.joinpath('micro_yaml.py')
	globs = {}
	code = compile(src.read_text(), str(src), 'exec')
	exec(code, globs)
	return globs['loads']


manifest = load_micro_yaml()(
	genvm_root_dir.joinpath('data', 'manifest.yaml').read_text()
)


def _load_registry(file: str | Path) -> dict[str, list[str]]:
	with open(file, 'r') as f:
		contents = json.load(f)

	if not isinstance(contents, dict):
		raise RuntimeError('expected dict for registry')

	ret: dict[str, list[str]] = {}

	for k, v in sorted(contents.items()):
		if isinstance(v, str):
			ret[k] = [v]
		elif isinstance(v, list):
			if not all([isinstance(x, str) for x in v]):
				raise RuntimeError(f'registry value must be str | list[str] for {k}')
			ret[k] = v

	for v in ret.values():
		v.sort()

	return ret


def _download_url(url: str) -> bytes:
	import urllib.request

	logger.info(f'downloading {url}')
	for attempt in range(5):
		try:
			with urllib.request.urlopen(url) as f:
				return f.read()
		except Exception as e:
			trace = traceback.format_exception(e)
			logger.warning(f'Attempt {attempt + 1} failed for {url}: {e}' + ''.join(trace))
	raise RuntimeError(f'failed to download {url} after multiple attempts')


def _download_template(descr: str, templates: list[str], vars: dict[str, str]) -> bytes:
	for url_template in templates:
		url = url_template.format(**vars)
		try:
			logger.info(f'downloading {url}')
			return _download_url(url)
		except Exception:
			pass
	raise RuntimeError(f'failed to download {descr}{vars} from all sources')


def download_runners_from_json(
	file: str | Path, runners_dir: Path, verify_hash: bool = True
):
	# `verify_hash` is disabled for the v0.2.x legacy line, whose registry hashes
	# use Nix base32 rather than the Crockford scheme `runner_check_bytes`
	# understands. Those runners are validated by the executor's own `check`
	# command (invoked via `manager check-install`) instead.
	file = Path(file)
	if not file.exists():
		if args.error_on_missing_executor:
			logger.error(f'Executor path {file} does not exist')
			raise RuntimeError(f'Executor path {file} does not exist')
		else:
			logger.warning(f'Executor path {file} does not exist, skipping')
			return
	logger.info(f'checking that all runners are present for {file}')
	all_runners = _load_registry(file)

	for name, hashes in all_runners.items():
		for hash in hashes:
			cur_dst = runners_dir.joinpath(name, hash[:2], hash[2:] + '.tar')

			if cur_dst.exists():
				if not verify_hash:
					logger.debug(f'already exists {name}:{hash}, skipping')
					continue
				data = cur_dst.read_bytes()
				if runner_check_bytes(data, hash):
					logger.debug(f'already exists {name}:{hash}, skipping')
					continue
				logger.warning(f'exists corrupted {name}:{hash}, removing')
				cur_dst.unlink()

			logger.debug(f'not found {cur_dst}')
			data = _download_template(
				f'runner {name}',
				manifest.get('runners_download_urls', []),
				{
					'name': name,
					'hash': hash,
					'hash_0_2': hash[:2],
					'hash_2_': hash[2:],
				},
			)
			if verify_hash and not runner_check_bytes(data, hash):
				raise ValueError(f'hash mismatch for {name}:{hash}')

			cur_dst.parent.mkdir(parents=True, exist_ok=True)
			cur_dst.write_bytes(data)


def download_executor(executor_version: str):
	"""Fetch an executor line from its own release and unpack it at the install root.

	Since the repo split the manager release carries no executors: each line is
	released separately, under its own executor-version. The tarball is laid out
	as `executor/<version>/...`, i.e. relative to the install root, so it is
	unpacked there.
	"""
	templates = manifest.get('executor_download_urls', [])
	if not templates:
		raise RuntimeError('manifest has no executor_download_urls')

	data = _download_template(
		f'executor {executor_version}',
		templates,
		{
			'version': executor_version,
			'platform': f'{args.arch}-{args.os}',
			'arch': args.arch,
			'os': args.os,
		},
	)

	with tarfile.open(fileobj=io.BytesIO(data), mode='r:xz') as tar:
		tar.extractall(genvm_root_dir, filter='data')

	logger.info(f'Unpacked executor {executor_version} into {genvm_root_dir}')


all_executor_versions = list(manifest.get('executor_versions', {}).keys())
all_executor_versions.sort()


def parse_executor_version(executor_version: str) -> tuple[int, int, int]:
	executor_version = executor_version.removeprefix('v')
	major_str, minor_str, patch_str = executor_version.split('.', 2)
	patch_str = patch_str.split('-', 1)[0]
	return (int(major_str), int(minor_str), int(patch_str))


all_executor_version_tuples = set()
for v in all_executor_versions:
	all_executor_version_tuples.add(parse_executor_version(v))


def process_executor_version(executor_version: str):
	logger.info(f'Examining executor version {executor_version}')

	major, minor, patch = parse_executor_version(executor_version)
	if (major, minor, patch + 1) in all_executor_version_tuples:
		logger.info(
			f'Skipping executor version {executor_version} because a newer version exists'
		)
		return

	executor_root_dir = genvm_root_dir.joinpath('executor', executor_version)
	executor_executable = executor_root_dir.joinpath('bin', 'genvm')

	# The manager bundle no longer ships the executors, so fetch what is missing
	# before anything below expects it on disk. A failure here is only fatal if a
	# missing executor is (--error-on-missing-executor).
	if args.executor_download and not executor_executable.exists():
		logger.info(f'Executor {executor_version} is not installed, downloading it')
		try:
			download_executor(executor_version)
		except Exception as e:
			if args.error_on_missing_executor:
				raise
			logger.warning(f'Could not download executor {executor_version}: {e}')

	# Refuse a wrong-OS executor before anything trusts it, regardless of which
	# steps run (a missing file is a no-op here; missing-file policy is handled
	# per-step below). Mirrors the unconditional modules_executable check.
	check_executable_platform(executor_executable)

	if patch_interpreter or args.bin_check:
		if not executor_executable.exists():
			if args.error_on_missing_executor:
				logger.error(f'Executor path {executor_executable} does not exist')
				raise RuntimeError(f'Executor path {executor_executable} does not exist')
			else:
				logger.warning(f'Executor path {executor_executable} does not exist, skipping')
				return
	if patch_interpreter:
		patch_executable(executor_executable)
	if args.bin_check:
		run_check_command([executor_executable, '--version'])

	if args.runners_download:
		# The v0.2.x legacy line keeps its runners private under the executor
		# root (executor/<version>/legacy-runners, see its genvm.yaml); every
		# other line shares the manager-root runners/ dir. v0.2.x also uses a
		# Nix-base32 registry hash this installer can't reproduce, so hash
		# verification is left to that executor's `check` command.
		is_legacy = (major, minor) == (0, 2)
		if is_legacy:
			runners_dir = executor_root_dir.joinpath('legacy-runners')
		else:
			runners_dir = genvm_root_dir.joinpath('runners')
		download_runners_from_json(
			executor_root_dir.joinpath('data', 'all.json'),
			runners_dir,
			verify_hash=not is_legacy,
		)


# The manifest is authoritative: only versions it lists are processed.
for executor_version in all_executor_versions:
	process_executor_version(executor_version)

# Verify installed runners (present, correct hashes, latest ⊆ all) and, when
# requested, precompile them. This is delegated to the manager's `check-install`
# subcommand, which fans out to each active executor's own `check` command
# instead of the executor being precompiled inline above.
if args.precompile:
	logger.info('Checking install and precompiling runners via manager')
	run_check_command([modules_executable, 'manager', 'check-install', '--precompile'])

# Warn about any executor directory present on disk that the manifest does not
# list (e.g. a stray or locally-built version); it is left untouched.
executor_parent = genvm_root_dir.joinpath('executor')
if executor_parent.is_dir():
	known = set(all_executor_versions)
	for entry in sorted(executor_parent.iterdir()):
		if entry.is_dir() and entry.name not in known:
			logger.warning(
				f'Executor directory {entry.name} is not listed in the manifest; skipping'
			)
