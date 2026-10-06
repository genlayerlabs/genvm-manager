#!/usr/bin/env python3
"""
Concatenate every ``*.txt`` under a directory into one bundle.

Used to fold a sphinx ``-b text`` build of one section into a single
``_static/ai/<section>.txt`` file (the "docs for LLMs" bundles). Each entry is
prefixed with its path relative to the source dir, files sorted for a stable
output.
"""

import sys
from pathlib import Path


def merge(src_dir: Path, dst_file: Path) -> None:
	buf: list[str] = []
	for file in sorted(src_dir.glob('**/*.txt')):
		name = file.relative_to(src_dir).as_posix()
		buf.append(f'### {name}\n\n')
		buf.append(file.read_text())
		buf.append('\n\n')
	dst_file.parent.mkdir(parents=True, exist_ok=True)
	dst_file.write_text(''.join(buf))


if __name__ == '__main__':
	merge(Path(sys.argv[1]), Path(sys.argv[2]))
