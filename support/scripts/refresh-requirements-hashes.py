#!/usr/bin/env python3

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

DEFAULT_REQUIREMENTS = (
	Path(__file__).parent.parent.parent
	/ 'install'
	/ 'lib'
	/ 'python'
	/ 'post-install'
	/ 'requirements.txt'
)

# `name==version` plus an optional `; marker`; anything else is not a pin we can
# hash (a range has no single set of files, and --require-hashes rejects it).
PIN = re.compile(r'^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s;\\]+)')


def fetch_hashes(name: str, version: str) -> list[str]:
	url = f'https://pypi.org/pypi/{name}/{version}/json'
	try:
		with urllib.request.urlopen(url) as f:
			doc = json.load(f)
	except Exception as e:
		raise RuntimeError(f'cannot read {url}: {e}') from e
	files = doc['urls']
	if not files:
		raise RuntimeError(f'PyPI publishes no files for {name}=={version}')
	return sorted(f['digests']['sha256'] for f in files)


def rewrite(text: str) -> str:
	out: list[str] = []
	for line in text.splitlines():
		stripped = line.strip()
		if stripped.startswith('--hash=') or stripped == '\\':
			continue
		if not stripped or stripped.startswith('#'):
			out.append(line)
			continue
		pin = PIN.match(stripped)
		if pin is None:
			raise RuntimeError(
				f'{stripped!r} is not a `name==version` pin; --require-hashes needs one'
			)
		hashes = fetch_hashes(pin['name'], pin['version'])
		print(f'{pin["name"]}=={pin["version"]}: {len(hashes)} distributions')
		requirement = stripped.rstrip('\\').strip()
		out.append(requirement + ' \\')
		out.extend(f'    --hash=sha256:{h} \\' for h in hashes[:-1])
		out.append(f'    --hash=sha256:{hashes[-1]}')
	return '\n'.join(out) + '\n'


def main() -> int:
	parser = argparse.ArgumentParser(
		description='Refresh the --hash lines of a pinned requirements file from PyPI'
	)
	parser.add_argument(
		'requirements',
		type=Path,
		nargs='?',
		default=DEFAULT_REQUIREMENTS,
		help='requirements file to rewrite in place',
	)
	parser.add_argument(
		'--check',
		action='store_true',
		help='do not write; exit non-zero if the file is out of date',
	)
	args = parser.parse_args()

	old = args.requirements.read_text()
	new = rewrite(old)

	if args.check:
		if old != new:
			print(f'{args.requirements}: hashes are out of date')
			return 1
		return 0

	if old != new:
		args.requirements.write_text(new)
		print(f'{args.requirements}: updated')
	else:
		print(f'{args.requirements}: already up to date')
	return 0


if __name__ == '__main__':
	sys.exit(main())
