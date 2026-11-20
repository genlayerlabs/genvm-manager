import argparse
import logging
import os
import platform
import shlex
import shutil
import struct
import subprocess
import typing
from pathlib import Path

host_os = platform.system().lower()
if host_os == 'darwin':
	host_os = 'macos'
if host_os not in ('linux', 'macos'):
	host_os = 'linux'  # default to linux


host_arch = platform.machine()
host_arch = {
	'x86_64': 'amd64',
	'aarch64': 'arm64',
	'arm64': 'arm64',
	'amd64': 'amd64',
}.get(host_arch, 'amd64')


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
	default=host_os,
	help='Target operating system (linux/macos)',
)
parser.add_argument(
	'--arch',
	type=str,
	default=host_arch,
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
	default=True,
	help='Ignored: patchelf is the only patcher',
)
parser.add_argument(
	'--create-venv',
	type=str_to_bool,
	default=False,
	help='Ignored: no virtual environment is needed',
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
	help='Ignored: runners ship in the release, nothing is downloaded',
)
parser.add_argument(
	'--runners-download',
	type=str_to_bool_or_none,
	default=None,
	help='Ignored: runners ship in the release, nothing is downloaded',
)
parser.add_argument(
	'--bin-patch',
	type=str_to_bool_or_none,
	default=None,
	help=(
		'Enable/disable pointing Linux binaries at the bundled lib/libc.so when '
		'their interpreter is missing on this host; that needs patchelf on PATH '
		'(default: use --default-steps)'
	),
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

for flag in ('default_download', 'runners_download'):
	if getattr(args, flag):
		logger.warning(
			f'--{flag.replace("_", "-")} is ignored: runners ship in the release '
			'(genvm-universal.tar.xz), nothing is downloaded'
		)

# Interpreter patching points the ELF interpreter (dynamic loader) at the bundled
# musl loader when the one baked in at build time is absent on this host. RPATH
# is $ORIGIN-relative and needs no install-time fixup; Mach-O has no loader path.
patch_interpreter = args.bin_patch and args.os == 'linux'

if patch_interpreter and (args.os, args.arch) != (host_os, host_arch):
	raise RuntimeError(
		f'cannot patch a {args.os}/{args.arch} tree on a {host_os}/{host_arch} host; '
		'pass --bin-patch=false'
	)

genvm_root_dir = Path(__file__).parent.parent.parent.parent
logger.info(f'Executor root directory: {genvm_root_dir}')

PT_INTERP = 3
ELF_MACHINES = {62: 'amd64', 183: 'arm64'}


def read_elf_interpreter(path: Path) -> tuple[str | None, str]:
	"""
	Read an ELF file's PT_INTERP and architecture without any tool. The
	interpreter is None for a statically linked binary."""
	with open(path, 'rb') as f:
		ident = f.read(16)
		if ident[:4] != b'\x7fELF' or ident[4] != 2:
			raise RuntimeError(f'{path} is not a 64-bit ELF binary')
		endian = '<' if ident[5] == 1 else '>'
		header = f.read(48)
		(e_machine,) = struct.unpack_from(endian + 'H', header, 2)
		(e_phoff,) = struct.unpack_from(endian + 'Q', header, 16)
		e_phentsize, e_phnum = struct.unpack_from(endian + 'HH', header, 38)
		arch = ELF_MACHINES.get(e_machine, f'machine-{e_machine}')
		for i in range(e_phnum):
			f.seek(e_phoff + i * e_phentsize)
			p_type, _, p_offset, _, _, p_filesz = struct.unpack(endian + 'IIQQQQ', f.read(40))
			if p_type == PT_INTERP:
				f.seek(p_offset)
				return f.read(p_filesz).split(b'\0', 1)[0].decode(), arch
	return None, arch


def patch_executable(path: Path):
	logger.info(f'Checking interpreter of {path}')
	if not path.exists():
		logger.warning(f'Path {path} does not exist, skipping patching')
		return

	if detect_executable_platform(path) != 'linux':
		logger.info(f'{path} is not ELF, nothing to patch')
		return

	interpreter, arch = read_elf_interpreter(path)
	if arch != host_arch:
		raise RuntimeError(f'{path} is a {arch} binary on a {host_arch} host')
	if interpreter is None:
		logger.info(f'{path} has no interpreter, nothing to patch')
		return
	if Path(interpreter).exists():
		logger.info(f'Interpreter {interpreter} exists, skipping interpreter patching')
		return

	loader = genvm_root_dir.joinpath('lib', 'libc.so').absolute()
	if not loader.exists():
		raise RuntimeError(
			f'interpreter {interpreter} is missing and {loader} does not exist'
		)
	patchelf = shutil.which('patchelf')
	if patchelf is None:
		raise RuntimeError(
			f'interpreter {interpreter} of {path} does not exist on this host, '
			'so it must be patched, but patchelf is not on PATH'
		)
	subprocess.run([patchelf, '--set-interpreter', str(loader), str(path)], check=True)
	logger.info(f'Patched interpreter of {path}: {interpreter} -> {loader}')


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
	"""
	Sniff a binary's header to identify its target OS: ELF -> linux, Mach-O
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
	"""
	Refuse a wrong-OS binary before we trust it. A bad release once shipped a
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


# The manifest is authoritative: only versions it lists are processed.
for executor_version in all_executor_versions:
	process_executor_version(executor_version)

# Verify installed runners (well-formed registry, present, correct hashes) and, when
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
