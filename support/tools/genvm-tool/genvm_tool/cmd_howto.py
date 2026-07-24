"""`genvm-tool howto` — list the contributing how-to guides.

Walks `docs/contributing/howto/`, reads each guide's one-line description from the
`README.md` index, and prints a grouped markdown listing of every guide. Exits
non-zero if a guide is missing from the index, or the index lists a guide that no
longer exists, so CI can gate on the index staying in sync.
"""

import re
import sys
from pathlib import Path

from . import common

NAME = 'howto'
HELP = 'render the contributing how-to guides as a skill-like index'

HOWTO_DIR = Path('docs/contributing/howto')
# Deepest directory nesting promoted to a `## header`; anything below stays in the
# guide's link text instead of spawning ever-deeper sections.
MAX_HEADER_DEPTH = 3
# README index lines look like: `- [text](rel/target.md) — one-line description.`
# `rel/target.md` is relative to the howto dir; the em dash (—) is the separator.
_INDEX_LINE = re.compile(r'-\s*\[[^\]]*\]\(([^)]+)\)\s*—\s*(.+)$')


def configure(parser):
	pass


def _parse_index(readme: Path) -> dict[str, str]:
	"""Map each README-listed target (howto-relative) to its one-line description."""
	descriptions: dict[str, str] = {}
	for line in readme.read_text().splitlines():
		m = _INDEX_LINE.match(line.strip())
		if m:
			descriptions[m.group(1)] = m.group(2).strip().rstrip('.')
	return descriptions


def _h1_title(path: Path) -> str:
	"""First `# heading` in a markdown file, or '' if it has none."""
	for line in path.read_text().splitlines():
		s = line.strip()
		if s.startswith('# '):
			return s[2:].strip()
	return ''


def main(ctx: common.Context, args) -> int:
	base = ctx.root / HOWTO_DIR
	if not base.is_dir():
		raise common.ToolError(f'not found: {HOWTO_DIR}')

	index = base / 'README.md'
	descriptions = _parse_index(index)

	# The top-level README *is* the index we render, not a guide within it.
	guides = [p for p in base.rglob('*') if p.is_file() and p != index]
	on_disk = {p.relative_to(base).as_posix() for p in guides}

	rows = []
	for path in guides:
		parts = path.relative_to(base).parts
		cut = min(len(parts) - 1, MAX_HEADER_DEPTH)  # dir segments that become a header
		header = '/'.join(parts[:cut])
		link = '/'.join(parts[cut:])
		desc = descriptions.get(path.relative_to(base).as_posix()) or _h1_title(path)
		rows.append((header, link, path.relative_to(ctx.root).as_posix(), desc))

	# '' (root) sorts before any header; within a section, by link text.
	current = None
	started = False
	for header, link, repo_path, desc in sorted(rows):
		if header != current:
			current = header
			if header:
				if started:
					print()  # blank line between sections, but not a leading one
				print(f'## {header[:1].upper()}{header[1:]}')
		print(f'- [{link}]({repo_path}) — {desc}')
		started = True

	index_rel = index.relative_to(ctx.root).as_posix()
	problems = [
		f'{index_rel}: missing entry for {rel}'
		for rel in sorted(on_disk - descriptions.keys())
	] + [
		f'{index_rel}: lists {rel}, which does not exist'
		for rel in sorted(descriptions.keys() - on_disk)
	]
	for problem in problems:
		print(problem, file=sys.stderr)
	return 1 if problems else 0
