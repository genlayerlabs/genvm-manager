#!/usr/bin/env python3
"""
Pre-commit guard: verify local file-path links in Markdown resolve.

Validates that **local** link targets (and reference definitions) point at
existing files; ignores external links (http(s)/mailto/other schemes),
protocol-relative `//host` links, and in-page `#anchor` links. Replaces the
network `markdown-link-check`.

Invoked by the git-hooks `markdown-local-links` hook with the changed markdown
files as arguments; the repo root is the current working directory (pre-commit
runs hooks from the repo top level).
"""

import os
import re
import sys
import urllib.parse
from pathlib import Path

# Drop fenced code blocks so ``` ...[x](y)... ``` samples are not scanned.
_FENCE = re.compile(r'```.*?```', re.DOTALL)
_INLINE = re.compile(r'!?\[[^\]]*\]\(([^)]+)\)')
_REFDEF = re.compile(r'(?m)^[ \t]*\[[^\]]+\]:[ \t]*(\S+)')
_SCHEME = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.\-]*:')  # http:, mailto:, ...


def _targets(text: str):
	text = _FENCE.sub('', text)
	for m in _INLINE.finditer(text):
		yield m.group(1)
	for m in _REFDEF.finditer(text):
		yield m.group(1)


def _clean(target: str) -> str:
	t = target.strip()
	if t.startswith('<') and t.endswith('>'):
		# Angle-bracketed targets may contain spaces; take them verbatim.
		t = t[1:-1]
	else:
		# Drop an optional "title" suffix: [x](path "title") / (path 'title').
		t = re.sub(r"""\s+["'].*$""", '', t)
	t = t.split('#', 1)[0]  # drop an in-page anchor: path#section -> path
	return urllib.parse.unquote(t)  # %20 -> space, etc.


def _is_local(t: str) -> bool:
	if not t or t.startswith('#') or t.startswith('//'):
		return False
	return not _SCHEME.match(t)


def _rel(p: Path, root: Path) -> str:
	return os.path.relpath(os.path.normpath(p), root)


def broken_links(md: Path, root: Path) -> list[str]:
	"""
	Return human-readable messages for each broken local link in `md`.

	Targets resolve relative to the markdown file's directory; a leading `/` is
	repo-root-relative (`root`). Paths in messages are shown relative to `root`.
	"""
	text = md.read_text(encoding='utf-8', errors='replace')
	base = md.parent
	out = []
	for raw in _targets(text):
		t = _clean(raw)
		if not _is_local(t):
			continue
		dest = (root / t.lstrip('/')) if t.startswith('/') else (base / t)
		if not dest.exists():
			out.append(
				f'{_rel(md, root)}: broken local link {raw!r} -> {_rel(dest, root)} (not found)'
			)
	return out


def main(argv: list[str]) -> int:
	root = Path.cwd()
	problems = []
	for arg in argv:
		problems += broken_links(Path(arg), root)
	for p in problems:
		print(p, file=sys.stderr)
	return 1 if problems else 0


if __name__ == '__main__':
	raise SystemExit(main(sys.argv[1:]))
