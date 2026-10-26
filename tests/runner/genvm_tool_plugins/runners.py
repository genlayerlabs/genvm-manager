import functools
import json
import re
from pathlib import Path

# `@RUNNER_LATEST_<line>_<runner>@`, where `<line>` is `v0.3` or `v0.3.x` and
# `<runner>` is a key of that line's `latest.json`, e.g. `py-genlayer`.
TOKEN = re.compile(rb'@RUNNER_LATEST_(v\d+\.\d+(?:\.x)?)_([A-Za-z0-9_.-]+)@')


def _line(line: str) -> str:
	return line[:-2] if line.endswith('.x') else line


@functools.cache
def latest(root_dir: Path, line: str) -> dict[str, str]:
	"""Runner id -> uid for the newest runners `line`'s executor was built with."""
	info_path = root_dir / 'build' / 'info.json'
	versions = json.loads(info_path.read_text())['executor_versions']
	version = versions.get(_line(line))
	if version is None:
		known = ', '.join(sorted(versions)) or '<none>'
		raise KeyError(f'unknown executor line {line!r}, built lines are {known}')
	path = root_dir / 'build' / 'out' / 'executor' / version / 'data' / 'latest.json'
	if not path.exists():
		raise FileNotFoundError(
			f'{path} is absent: build the runners before running this test'
		)
	return json.loads(path.read_text())


def uid(root_dir: Path, line: str, runner: str) -> str:
	"""Uid of one runner, as `latest.json` has it after the last build."""
	known = latest(root_dir, line)
	if runner not in known:
		raise KeyError(
			f'{line} has no runner {runner!r}, it has {", ".join(sorted(known))}'
		)
	return known[runner]


def substitute(data: bytes, root_dir: Path) -> bytes:
	"""
	Replaces every `@RUNNER_LATEST_<line>_<runner>@` with the built uid.

	A runner's uid changes with its contents, so a test that pins one by hand
	goes stale on every runner edit. The token spares the test that pin.
	"""

	def replace(match: re.Match[bytes]) -> bytes:
		line = match.group(1).decode('utf-8')
		runner = match.group(2).decode('utf-8')
		return uid(root_dir, line, runner).encode('utf-8')

	return TOKEN.sub(replace, data)
