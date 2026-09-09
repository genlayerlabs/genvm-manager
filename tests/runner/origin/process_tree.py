"""Portable process-tree inspection for lifecycle integration assertions."""

import subprocess
from collections.abc import Iterable
from pathlib import Path


def descendants_from_pairs(pid: int, pairs: Iterable[tuple[int, int]]) -> set[int]:
	children: dict[int, set[int]] = {}
	for child, parent in pairs:
		children.setdefault(parent, set()).add(child)
	result: set[int] = set()
	pending = [pid]
	while pending:
		parent = pending.pop()
		for child in children.get(parent, ()):
			if child not in result:
				result.add(child)
				pending.append(child)
	return result


def _proc_pairs(proc_root: Path) -> Iterable[tuple[int, int]]:
	for stat_path in proc_root.glob('[0-9]*/stat'):
		try:
			raw = stat_path.read_text()
			parent = int(raw[raw.rfind(')') + 2 :].split()[1])
			child = int(stat_path.parent.name)
		except (FileNotFoundError, ProcessLookupError, ValueError):
			continue
		yield child, parent


def _ps_pairs() -> Iterable[tuple[int, int]]:
	rows = subprocess.run(
		['ps', '-axo', 'pid=,ppid='],
		check=True,
		capture_output=True,
		text=True,
	).stdout
	for row in rows.splitlines():
		try:
			child, parent = (int(value) for value in row.split())
		except ValueError:
			continue
		yield child, parent


def descendants(pid: int, proc_root: Path = Path('/proc')) -> set[int]:
	"""Return every transitive child of ``pid`` on Linux and macOS."""

	pairs = _proc_pairs(proc_root) if proc_root.is_dir() else _ps_pairs()
	return descendants_from_pairs(pid, pairs)
