import argparse
import json
import os
import re
import sys

import ci_lib


def bare(version: str) -> str:
	return version.lstrip('v').split('-', 1)[0]


def major_minor(version: str) -> str:
	return '.'.join(bare(version).split('.')[:2])


def version_key(version: str) -> tuple[int, ...]:
	return tuple(int(part) for part in major_minor(version).split('.'))


def load_root_conf() -> dict:
	conf_path = os.environ.get('MONOREPO_ROOT', ci_lib.ROOT_DIR / '.genvm-monorepo-root')
	return json.loads(open(conf_path, 'rb').read())


def active_versions() -> list[str]:
	conf = load_root_conf()
	return sorted(
		(bare(version) for version in conf.get('active-versions', [])), key=version_key
	)


def manager_version() -> str:
	"""The manager's own release train as bare major.minor (e.g. '0.6').

	Read from the `version` field. The manager dev/release branches
	(`v<X>-dev`, `v<X>.x`) are named after this — NOT after the executor lines
	in `active-versions`, which move independently (currently v0.2 / v0.3).
	"""
	return major_minor(load_root_conf()['version'])


def active_lines() -> list[str]:
	"""Active executor lines as `v<X>` tags (e.g. ['v0.2', 'v0.3']).

	Same data as `active_versions`, prefixed with the `v` the executor branches
	(`<line>-dev`, `pr/<line>/...`) use. The manager release line is independent of
	these; a manager PR ships every active line.
	"""
	return [f'v{version}' for version in active_versions()]


class BranchVersions(ci_lib.Tool):
	"""Read version trains from .genvm-monorepo-root.

	`list` yields the active executor lines (`active-versions`); `manager`
	yields the manager's own train (`version`). These differ — don't use one
	where the other is meant.
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
	"""Keep manager-owned Rust crate versions in sync with .genvm-monorepo-root."""

	CARGO_FILES = ['implementation/Cargo.toml']

	def name(self) -> str:
		return 'check-versions'

	def add_to(self, parser: argparse.ArgumentParser) -> None:
		sub = parser.add_subparsers(dest='cmd')
		sub.add_parser('sync', help='rewrite crate versions to match version')
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
		active = conf.get('active-versions', [])
		if not active:
			print('no active versions configured in .genvm-monorepo-root', file=sys.stderr)
			return 1
		latest = sorted(active, key=version_key)[-1]

		if set_version is not None:
			new_mm = bare(set_version)
			if not re.fullmatch(r'\d+\.\d+', new_mm):
				print(f'--set expects major.minor, got {set_version!r}', file=sys.stderr)
				return 2
		else:
			major, minor = (int(part) for part in major_minor(latest).split('.'))
			if level == 'minor':
				minor += 1
			else:
				major += 1
				minor = 0
			new_mm = f'{major}.{minor}'

		new = f'v{new_mm}'
		if new in active:
			print(f'version {new} is already active', file=sys.stderr)
			return 1
		if version_key(new) <= version_key(latest):
			print(f'new version {new} is not greater than latest {latest}', file=sys.stderr)
			return 1

		text = self.conf_path().read_text()
		new_active = '[' + ', '.join(f'"{version}"' for version in active + [new]) + ']'
		text, n1 = re.subn(r'("version"\s*:\s*)"[^"]*"', rf'\g<1>"{new}"', text)
		text, n2 = re.subn(
			r'("active-versions"\s*:\s*)\[[^\]]*\]',
			lambda match: match.group(1) + new_active,
			text,
		)
		if n1 != 1 or n2 != 1:
			print(
				f'failed to patch .genvm-monorepo-root (version={n1}, active-versions={n2})',
				file=sys.stderr,
			)
			return 1
		self.conf_path().write_text(text)

		for cargo_file in self.CARGO_FILES:
			if self.set_package_version(cargo_file, f'{new_mm}.0') is None:
				print(
					f'{cargo_file}: could not find [package] version to bump', file=sys.stderr
				)
				return 1
		print(new_mm)
		return 0

	def handler(self, args: argparse.Namespace) -> int:
		if args.cmd == 'bump':
			return self.bump(args.level, args.set_version)
		return self.sync()


COMMANDS = [BranchVersions(), CheckVersions()]
