import argparse
import json
import os
import re
import sys
from pathlib import Path

import ci_lib


def bare(version: str) -> str:
	return version.lstrip('v').split('-', 1)[0]


def major_minor(version: str) -> str:
	return '.'.join(bare(version).split('.')[:2])


def version_key(version: str) -> tuple[int, ...]:
	return tuple(int(part) for part in major_minor(version).split('.'))


LINE_STATUSES = ('preview', 'current', 'maintained', 'replay-only')


class LinesError(Exception):
	pass


def load_root_conf() -> dict:
	conf_path = os.environ.get('MONOREPO_ROOT', ci_lib.ROOT_DIR / '.genvm-monorepo-root')
	return json.loads(Path(conf_path).read_bytes())


def executor_lines() -> dict[str, str]:
	"""
	`executor-lines` as `{line: status}`, ascending by version.

	Raises `LinesError` unless there is exactly 1 `current` line, which the
	schema cannot express. Line names and statuses are rechecked, so a reader
	of an unchecked file fails here instead of on a later lookup.
	"""
	lines = load_root_conf().get('executor-lines')
	if not isinstance(lines, dict):
		raise LinesError('`executor-lines` must be an object')
	errors = []
	for line, entry in lines.items():
		if not re.fullmatch(r'v\d+\.\d+', line):
			errors.append(f'`{line}`: a line must be `v<major>.<minor>`')
		elif not isinstance(entry, dict):
			errors.append(f'{line}: must be an object with a `status`')
		elif entry.get('status') not in LINE_STATUSES:
			errors.append(f'{line}: unknown status `{entry.get("status")}`')
	if errors:
		raise LinesError('\n'.join(errors))
	current = sorted(
		line for line, entry in lines.items() if entry['status'] == 'current'
	)
	if len(current) != 1:
		raise LinesError(f'exactly 1 `current` line, found {", ".join(current) or "none"}')
	return {line: lines[line]['status'] for line in sorted(lines, key=version_key)}


def active_versions() -> list[str]:
	return [bare(line) for line in executor_lines()]


def manager_version() -> str:
	"""
	The manager's own release train as bare major.minor (e.g. '0.6').

	Read from the `version` field. The manager dev/release branches
	(`v<X>-dev`, `v<X>`) are named after this — NOT after `executor-lines`,
	which move independently.
	"""
	return major_minor(load_root_conf()['version'])


def active_lines() -> list[str]:
	"""
	Every executor line as a `v<X>` tag (e.g. ['v0.2', 'v0.3']), whatever its status.

	The form the executor branches (`<line>-dev`, `pr/<line>/...`) use; a manager
	PR ships every line.
	"""
	return list(executor_lines())


def current_line() -> str:
	"""The `current` executor line as `v<X>`, which owns the manager-global files."""
	return next(line for line, status in executor_lines().items() if status == 'current')


class BranchVersions(ci_lib.Tool):
	"""
	Read version trains from .genvm-monorepo-root.

	`list` yields the executor lines (`executor-lines`); `manager` yields the
	manager's own train (`version`). These differ — don't use one where the
	other is meant.
	"""

	def name(self) -> str:
		return 'branch-versions'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		parser.add_argument('cmd', nargs='?', choices=['list', 'manager'], default='list')

	def handler(self, args: argparse.Namespace) -> int:
		if args.cmd == 'manager':
			print(manager_version())
			return 0
		print('\n'.join(active_versions()))
		return 0


class CheckVersions(ci_lib.Tool):
	"""
	Keep manager-owned Rust crate versions in sync with .genvm-monorepo-root.
	"""

	CARGO_FILES = ['implementation/Cargo.toml']

	def name(self) -> str:
		return 'check-versions'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		sub = parser.add_subparsers(dest='cmd')
		sub.add_parser('sync', help='rewrite crate versions to match version')
		sub.add_parser('lines', help='check the executor-lines rules the schema cannot')
		bump = sub.add_parser('bump', help='cut the next version train')
		bump.add_argument('level', nargs='?', choices=['minor', 'major'], default='minor')
		bump.add_argument('--set', dest='set_version', metavar='MAJOR.MINOR')

	def conf_path(self):
		return ci_lib.ROOT_DIR / '.genvm-monorepo-root'

	def load_conf(self):
		return json.loads(self.conf_path().read_bytes())

	def package_version(self, path):
		section = None
		for line in path.read_text().splitlines():
			stripped = line.strip()
			match = re.match(r'\[(.+?)\]', stripped)
			if match:
				section = match.group(1)
				continue
			if section == 'package':
				match = re.match(r'version\s*=\s*"([^"]+)"', stripped)
				if match:
					return match.group(1)
		return None

	def set_package_version(self, rel: str, full: str):
		path = ci_lib.ROOT_DIR / rel
		old = None
		out = []
		section = None
		for line in path.read_text().splitlines(keepends=True):
			match = re.match(r'\s*\[(.+?)\]', line)
			if match:
				section = match.group(1)
			elif section == 'package' and old is None:
				match = re.match(r'(version\s*=\s*)"([^"]*)"', line)
				if match:
					old = match.group(2)
					line = re.sub(r'"[^"]*"', f'"{full}"', line, count=1)
			out.append(line)
		if old is None:
			return None
		path.write_text(''.join(out))
		return old

	def sync(self) -> int:
		mm = major_minor(self.load_conf()['version'])
		rc = 0
		for cargo_file in self.CARGO_FILES:
			current = self.package_version(ci_lib.ROOT_DIR / cargo_file)
			if current is None:
				print(f'{cargo_file}: could not find [package] version')
				rc = 1
				continue
			parts = current.split('.', 2)
			if '.'.join(parts[:2]) == mm:
				continue
			patch = parts[2] if len(parts) == 3 else '0'
			target = f'{mm}.{patch}'
			self.set_package_version(cargo_file, target)
			print(
				f'{cargo_file}: bumped {current} -> {target} to match .genvm-monorepo-root version ({mm})'
			)
			rc = 1
		return rc

	def bump(self, level: str, set_version: str | None) -> int:
		conf = self.load_conf()
		current = major_minor(conf['version'])

		if set_version is not None:
			new_mm = set_version.removeprefix('v')
			if not re.fullmatch(r'\d+\.\d+', new_mm):
				print(f'--set expects major.minor, got {set_version!r}', file=sys.stderr)
				return 2
		else:
			major, minor = (int(part) for part in current.split('.'))
			if level == 'minor':
				minor += 1
			else:
				major += 1
				minor = 0
			new_mm = f'{major}.{minor}'

		if version_key(new_mm) <= version_key(current):
			print(
				f'new train {new_mm} is not greater than current {current}', file=sys.stderr
			)
			return 1
		for cargo_file in self.CARGO_FILES:
			if self.package_version(ci_lib.ROOT_DIR / cargo_file) is None:
				print(
					f'{cargo_file}: could not find [package] version to bump', file=sys.stderr
				)
				return 1

		conf['version'] = f'v{new_mm}.0-rc0'
		self.conf_path().write_text(json.dumps(conf, indent=4) + '\n')
		for cargo_file in self.CARGO_FILES:
			self.set_package_version(cargo_file, f'{new_mm}.0')
		print(new_mm)
		return 0

	def handler(self, args: argparse.Namespace) -> int:
		if args.cmd == 'bump':
			return self.bump(args.level, args.set_version)
		if args.cmd == 'lines':
			try:
				executor_lines()
			except LinesError as error:
				print(f'.genvm-monorepo-root: {error}')
				return 1
			return 0
		return self.sync()


COMMANDS = [BranchVersions(), CheckVersions()]
