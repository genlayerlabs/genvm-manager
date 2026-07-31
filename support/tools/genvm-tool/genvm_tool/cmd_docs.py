"""`genvm-tool docs` — list the contributor documentation.

Walks each quadrant of `docs/contributing/` (tutorial, how-to, explanation), reads
every page's one-line description from that quadrant's `README.md` index, and prints
a grouped markdown listing. Exits non-zero if a page is missing from its index, or an
index lists a page that no longer exists, so CI can gate on the indexes staying in
sync.
"""

import re
import sys
from pathlib import Path

from . import common

NAME = 'docs'
HELP = 'render the contributor documentation as a skill-like index'

CONTRIBUTING_DIR = Path('docs/contributing')
# Rendered in this order: learn it, do it, understand it.
QUADRANTS = ['tutorial', 'howto', 'explanation']
# Deepest directory nesting promoted to a `## header`; anything below stays in the
# page's link text instead of spawning ever-deeper sections.
MAX_HEADER_DEPTH = 3
# README index lines look like: `- [text](rel/target.md) — one-line description.`
# `rel/target.md` is relative to the quadrant dir; the em dash (—) is the separator.
_INDEX_LINE = re.compile(r'-\s*\[[^\]]*\]\(([^)]+)\)\s*—\s*(.+)$')


def configure(parser):
	pass


def _parse_index(readme: Path) -> dict[str, str]:
	"""Map each README-listed target (quadrant-relative) to its one-line description."""
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


def _rows(ctx: common.Context, base: Path) -> tuple[list, list[str]]:
	"""Listing rows for one quadrant, plus its index-vs-disk complaints."""
	index = base / 'README.md'
	if not index.is_file():
		raise common.ToolError(f'not found: {index.relative_to(ctx.root).as_posix()}')
	descriptions = _parse_index(index)

	# The quadrant README *is* the index being rendered, not a page within it.
	pages = [p for p in base.rglob('*') if p.is_file() and p != index]
	on_disk = {p.relative_to(base).as_posix() for p in pages}

	rows = []
	for path in pages:
		parts = path.relative_to(base).parts
		cut = min(len(parts) - 1, MAX_HEADER_DEPTH)  # dir segments that become a header
		header = '/'.join(parts[:cut])
		link = '/'.join(parts[cut:])
		desc = descriptions.get(path.relative_to(base).as_posix()) or _h1_title(path)
		rows.append((header, link, path.relative_to(ctx.root).as_posix(), desc))

	index_rel = index.relative_to(ctx.root).as_posix()
	problems = [
		f'{index_rel}: missing entry for {rel}'
		for rel in sorted(on_disk - descriptions.keys())
	] + [
		f'{index_rel}: lists {rel}, which does not exist'
		for rel in sorted(descriptions.keys() - on_disk)
	]
	return rows, problems


def _section_title(quadrant: str, header: str) -> str:
	"""`howto`, `building` -> `Howto/Building`; the empty header is the quadrant root."""
	parts = [quadrant] + ([header] if header else [])
	return '/'.join(f'{p[:1].upper()}{p[1:]}' for p in '/'.join(parts).split('/'))


def main(ctx: common.Context, args) -> int:
	problems = []
	started = False
	for quadrant in QUADRANTS:
		base = ctx.root / CONTRIBUTING_DIR / quadrant
		if not base.is_dir():
			raise common.ToolError(f'not found: {CONTRIBUTING_DIR / quadrant}')

		rows, quadrant_problems = _rows(ctx, base)
		problems += quadrant_problems

		# '' (the quadrant root) sorts before any header; within a section, by link text.
		current = None
		for header, link, repo_path, desc in sorted(rows):
			if header != current:
				current = header
				if started:
					print()  # blank line between sections, but not a leading one
				print(f'## {_section_title(quadrant, header)}')
			print(f'- [{link}]({repo_path}) — {desc}')
			started = True

	for problem in problems:
		print(problem, file=sys.stderr)
	return 1 if problems else 0
