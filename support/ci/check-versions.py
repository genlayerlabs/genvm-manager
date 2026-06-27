#!/usr/bin/env python3

"""
Keep the Rust crate versions in sync with .genvm-monorepo-root.

	check-versions.py [sync]
		# pre-commit fixer: rewrite each crate's
		# [package] major.minor to match
		# .genvm-monorepo-root `version` (patch
		# kept). Exits non-zero if it changed
		# anything, so the commit is re-staged.
	check-versions.py bump minor   # cut next train: 0.3 -> 0.4 (default)
	check-versions.py bump major   # 0.3 -> 1.0
	check-versions.py bump --set 0.6   # explicit major.minor

`bump` updates `version`, appends the new train to `active-versions`,
sets each crate's [package] version to <new>.0 (preserving file
formatting) and prints the new major.minor.

The `version` / `active-versions` values may carry a leading `v` (and
`version` may carry a `-suffix`); both are stripped when a bare
major.minor is needed (crate versions, the printed train name).
"""

import argparse
import json
import pathlib
import re
import sys

root = pathlib.Path(__file__).resolve().parents[2]
conf_path = root / '.genvm-monorepo-root'

# Manager-owned crates only. The executor lives in a git submodule and is
# versioned independently (its own repo owns those crate versions); we never
# rewrite or mirror them from here.
CARGO_FILES = [
	'implementation/Cargo.toml',
]


def load_conf():
	return json.loads(conf_path.read_bytes())


def bare(v):
	"""Strip an optional leading 'v' and any '-suffix', e.g.
	'v0.3' -> '0.3', 'v0.3.2-dev' -> '0.3.2'."""
	return v.lstrip('v').split('-', 1)[0]


def to_mm(v):
	"""Normalize a version string to a bare major.minor."""
	return '.'.join(bare(v).split('.')[:2])


def package_version(path):
	section = None
	for line in path.read_text().splitlines():
		s = line.strip()
		m = re.match(r'\[(.+?)\]', s)
		if m:
			section = m.group(1)
			continue
		if section == 'package':
			m = re.match(r'version\s*=\s*"([^"]+)"', s)
			if m:
				return m.group(1)
	return None


def set_package_version(rel, full):
	"""Rewrite the [package] version of a crate to `full`. Returns the old
	version, or None if no [package] version line was found."""
	p = root / rel
	old = None
	out = []
	section = None
	for line in p.read_text().splitlines(keepends=True):
		m = re.match(r'\s*\[(.+?)\]', line)
		if m:
			section = m.group(1)
		elif section == 'package' and old is None:
			m = re.match(r'(version\s*=\s*)"([^"]*)"', line)
			if m:
				old = m.group(2)
				line = re.sub(r'"[^"]*"', f'"{full}"', line, count=1)
		out.append(line)
	if old is None:
		return None
	p.write_text(''.join(out))
	return old


def vkey(v):
	return tuple(int(x) for x in to_mm(v).split('.'))


def sync():
	"""Pre-commit fixer: force crate [package] major.minor to match
	`version`, preserving the patch component. Non-zero exit on any
	change (or error) so pre-commit re-stages the rewrite."""
	mm = to_mm(load_conf()['version'])
	rc = 0
	for f in CARGO_FILES:
		cur = package_version(root / f)
		if cur is None:
			print(f'{f}: could not find [package] version')
			rc = 1
			continue
		parts = cur.split('.', 2)
		cur_mm = '.'.join(parts[:2])
		if cur_mm == mm:
			continue
		patch = parts[2] if len(parts) == 3 else '0'
		target = f'{mm}.{patch}'
		set_package_version(f, target)
		print(f'{f}: bumped {cur} -> {target} to match .genvm-monorepo-root version ({mm})')
		rc = 1
	return rc


def bump(level, set_version):
	conf = load_conf()
	active = conf.get('active-versions', [])
	if not active:
		print('no active versions configured in .genvm-monorepo-root', file=sys.stderr)
		return 1
	latest = sorted(active, key=vkey)[-1]

	if set_version is not None:
		new_mm = bare(set_version)
		if not re.fullmatch(r'\d+\.\d+', new_mm):
			print(f'--set expects major.minor, got {set_version!r}', file=sys.stderr)
			return 2
	else:
		major, minor = (int(x) for x in to_mm(latest).split('.'))
		if level == 'minor':
			minor += 1
		else:
			major += 1
			minor = 0
		new_mm = f'{major}.{minor}'

	# Stored form keeps the leading `v` used by `active-versions` entries.
	new = f'v{new_mm}'

	if new in active:
		print(f'version {new} is already active', file=sys.stderr)
		return 1
	if vkey(new) <= vkey(latest):
		print(f'new version {new} is not greater than latest {latest}', file=sys.stderr)
		return 1

	# .genvm-monorepo-root — patch the two values in place, keep formatting.
	text = conf_path.read_text()
	new_active = '[' + ', '.join(f'"{v}"' for v in active + [new]) + ']'
	text, n1 = re.subn(r'("version"\s*:\s*)"[^"]*"', rf'\g<1>"{new}"', text)
	text, n2 = re.subn(
		r'("active-versions"\s*:\s*)\[[^\]]*\]', lambda m: m.group(1) + new_active, text
	)
	if n1 != 1 or n2 != 1:
		print(
			f'failed to patch .genvm-monorepo-root (version={n1}, active-versions={n2})',
			file=sys.stderr,
		)
		return 1
	conf_path.write_text(text)

	for f in CARGO_FILES:
		if set_package_version(f, f'{new_mm}.0') is None:
			print(f'{f}: could not find [package] version to bump', file=sys.stderr)
			return 1

	# Print the bare major.minor: callers build branch names as v${NEW}.
	print(new_mm)
	return 0


def main():
	parser = argparse.ArgumentParser(
		description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
	)
	sub = parser.add_subparsers(dest='cmd')
	sub.add_parser(
		'sync',
		help='rewrite crate versions to match version (default; pre-commit fixer)',
	)
	bp = sub.add_parser('bump', help='cut the next version train')
	bp.add_argument('level', nargs='?', choices=['minor', 'major'], default='minor')
	bp.add_argument(
		'--set',
		dest='set_version',
		metavar='MAJOR.MINOR',
		help='explicit version (overrides level)',
	)

	args = parser.parse_args()
	if args.cmd == 'bump':
		return bump(args.level, args.set_version)
	return sync()


if __name__ == '__main__':
	sys.exit(main())
