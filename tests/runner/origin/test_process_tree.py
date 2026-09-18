import os
import subprocess
from pathlib import Path

from . import process_tree


def test_descendants_from_pairs_walks_the_transitive_tree() -> None:
	assert process_tree.descendants_from_pairs(
		10,
		[(11, 10), (12, 11), (13, 12), (20, 99)],
	) == {11, 12, 13}


def test_descendants_falls_back_to_ps_without_procfs(
	monkeypatch,
	tmp_path: Path,
) -> None:
	def run_ps(*args, **kwargs):
		assert args == (['ps', '-axo', 'pid=,ppid='],)
		assert kwargs == {'check': True, 'capture_output': True, 'text': True}
		return subprocess.CompletedProcess(args[0], 0, stdout='11 10\n12 11\n')

	monkeypatch.setattr(process_tree.subprocess, 'run', run_ps)

	assert process_tree.descendants(10, tmp_path / 'missing-proc') == {11, 12}


def test_descendants_finds_a_live_child_on_this_platform() -> None:
	child = subprocess.Popen(['sleep', '10'])
	try:
		assert child.pid in process_tree.descendants(os.getpid())
	finally:
		child.terminate()
		child.wait(timeout=5)
