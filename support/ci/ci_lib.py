import abc
import argparse
import contextlib
import hashlib
import io
import lzma
import os
import shlex
import subprocess
import tarfile
import typing
from pathlib import Path


class Command(abc.ABC):
	@abc.abstractmethod
	def name(self) -> str: ...

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		pass

	@abc.abstractmethod
	def handler(self, args: argparse.Namespace) -> int | None: ...


class Pipeline(Command):
	pass


class Tool(Command):
	pass


ROOT_DIR = Path(__file__).resolve().parent

while ROOT_DIR != ROOT_DIR.parent:
	if (ROOT_DIR / '.genvm-monorepo-root').exists():
		break
	ROOT_DIR = ROOT_DIR.parent

if not (ROOT_DIR / '.genvm-monorepo-root').exists():
	raise RuntimeError(
		f'Could not find .genvm-monorepo-root in {ROOT_DIR} or any parent directory'
	)


def github_group_start(name: str):
	print(f'::group::{name}', flush=True)


def github_group_end():
	print('::endgroup::', flush=True)


@contextlib.contextmanager
def github_group(name: str):
	github_group_start(name)
	try:
		yield
	finally:
		github_group_end()


def github_error(
	message: str,
	*,
	file: str | Path | None = None,
	line: int | None = None,
	col: int | None = None,
) -> None:
	if line is not None:
		assert file is not None, 'line number provided but no file'
	if col is not None:
		assert line is not None, 'column number provided but no line number'

	msg_prefix = ''
	if file is not None:
		msg_prefix = f' file={file}'
		if line is not None:
			msg_prefix += f',line={line}'
		if col is not None:
			msg_prefix += f',col={col}'

	print(f'::error{msg_prefix}::{message.replace(chr(10), " ")}')


def run(
	command: list[str | Path],
	*,
	env: dict[str, str] | None = None,
	cwd: Path | None = None,
	check: bool = True,
	stdout=None,
	capture_output: bool = False,
) -> subprocess.CompletedProcess:
	if cwd is None:
		cwd = ROOT_DIR
	else:
		print(f'+ cd {shlex.quote(str(cwd.resolve()))}')
	print(f'+ {" ".join(shlex.quote(str(arg)) for arg in command)}', flush=True)

	full_env = None
	if env is not None:
		full_env = os.environ.copy()
		full_env.update(env)

	return subprocess.run(
		[str(arg) for arg in command],
		check=check,
		env=full_env,
		cwd=cwd,
		stdout=stdout,
		capture_output=capture_output,
		text=True,
	)


def output(command: list[str | Path], *, cwd: Path | None = None) -> str:
	return run(command, cwd=cwd, capture_output=True).stdout


_nix_develop_cache: set[str] = set()


def nix_develop(
	installable: str,
	cmd: typing.Sequence[str | Path],
	*,
	check: bool = True,
	fresh_env: bool = False,
	subcommand_group: str | None = None,
):
	cache_path = ROOT_DIR / '.direnv' / 'profile-cache'
	if not cache_path.exists():
		cache_path.mkdir(parents=True, exist_ok=True)
	digest = hashlib.sha3_256(installable.encode()).hexdigest()[:8]
	profile_path = cache_path / f'{digest}'
	if installable not in _nix_develop_cache or not profile_path.exists():
		with github_group(f'nix-develop pre-cache: {installable}'):
			run(
				['nix', 'develop', installable, '--profile', profile_path, '--command', 'true'],
			)
		_nix_develop_cache.add(installable)
	if not profile_path.exists():
		raise RuntimeError(f'nix-develop profile cache {profile_path} does not exist')

	print(f'# using {installable}')

	fresh_env_args = []
	if fresh_env:
		fresh_env_args = ['-i']

	if subcommand_group is not None:
		github_group_start(subcommand_group)
	try:
		run(
			['nix', 'develop', profile_path, *fresh_env_args, '--command', *cmd],
			check=check,
		)
	finally:
		if subcommand_group is not None:
			github_group_end()


def bundle_artifact(
	*,
	root_path: Path,
	result_path: Path,
	globs: list[str],
	compression_level: int,
):
	all_files: set[Path] = set()
	for pat in globs:
		all_files.update(root_path.glob(pat))

	all_files_det = list(sorted(all_files, key=lambda p: str(p)))

	ext = result_path.name.split('.')[-1]

	def _process_with_tar(tar_file: tarfile.TarFile):
		for path in all_files_det:
			if path.is_dir() and not path.is_symlink():
				continue
			stats = path.stat()
			rel_path = path.relative_to(root_path)
			info = tarfile.TarInfo()
			info.name = str(rel_path)
			info.type = tarfile.REGTYPE
			if stats.st_mode & 0o111:
				info.mode = 0o755
			else:
				info.mode = 0o644
			# if we can preserve as local symlink we preserve
			# otherwise we dereference
			if path.is_symlink():
				target = path.readlink()
				if not target.is_absolute():
					normalized = os.path.normpath(rel_path.parent / target)
					if normalized != '..' and not normalized.startswith('..' + os.sep):
						info.type = tarfile.SYMTYPE
						info.linkname = target.as_posix()
						info.size = 0
						tar_file.addfile(info)
						continue
			data = path.read_bytes()
			info.size = path.stat().st_size
			tar_file.addfile(info, io.BytesIO(data))

	match ext:
		case 'tar':
			file = open(result_path, 'wb')
		case 'xz':
			file = lzma.open(
				result_path, mode='wb', preset=compression_level, format=lzma.FORMAT_XZ
			)
		case _:
			raise ValueError(f'unsupported archive extension: {ext}')

	try:
		with tarfile.open(
			fileobj=file,
			mode='w',
			format=tarfile.USTAR_FORMAT,
			encoding='utf-8',
		) as tar_file:
			_process_with_tar(tar_file)
	finally:
		file.close()
