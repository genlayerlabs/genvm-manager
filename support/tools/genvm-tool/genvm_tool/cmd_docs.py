"""
`genvm-tool docs` — list the contributor documentation.

Walks each quadrant of `docs/contributing/` (tutorial, how-to, explanation), reads
every page's one-line description from that quadrant's `README.md` index, and prints
a grouped markdown listing. Exits non-zero if a page is missing from its index, or an
index lists a page that no longer exists, so CI can gate on the indexes staying in
sync.

`--write` splices the same listing into `AGENTS.md` below its generated marker, and
`--check` verifies that copy is current without touching it — the listing is what
every agent session loads, so a stale copy is a wrong answer, not a cosmetic one.
"""

import re
import sys
from pathlib import Path

from . import common

NAME = 'docs'
HELP = 'render the contributor documentation as a skill-like index'

CONTRIBUTING_DIR = Path('docs/contributing')
# The agent-instructions file and the marker below which its listing is generated.
# Everything above the marker is hand-maintained and never rewritten.
AGENTS_FILE = Path('AGENTS.md')
AGENTS_MARKER = '<!-- below is generated with `genvm-tool docs` -->'
# Rendered in this order: learn it, do it, understand it.
QUADRANTS = ['tutorial', 'howto', 'explanation']
# Deepest directory nesting promoted to a `## header`; anything below stays in the
# page's link text instead of spawning ever-deeper sections.
MAX_HEADER_DEPTH = 3
# README index lines look like: `- [text](rel/target.md) — one-line description.`
# `rel/target.md` is relative to the quadrant dir; the em dash (—) is the separator.
_INDEX_LINE = re.compile(r'-\s*\[[^\]]*\]\(([^)]+)\)\s*—\s*(.+)$')


def configure(parser):
	mode = parser.add_mutually_exclusive_group()
	mode.add_argument(
		'--write', action='store_true', help=f'update the listing in {AGENTS_FILE}'
	)
	mode.add_argument(
		'--check',
		action='store_true',
		help=f'fail if the listing in {AGENTS_FILE} is stale',
	)


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


def _listing(ctx: common.Context) -> tuple[str, list[str]]:
	"""The rendered markdown listing, plus every index-vs-disk complaint."""
	problems: list[str] = []
	lines: list[str] = []
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
				if lines:
					lines.append('')  # blank line between sections, but not a leading one
				lines.append(f'## {_section_title(quadrant, header)}')
			lines.append(f'- [{link}]({repo_path}) — {desc}')

	return '\n'.join(lines) + '\n', problems


def _spliced(ctx: common.Context, listing: str) -> tuple[Path, str, str]:
	"""`AGENTS.md` as it is on disk and as it should be, with the path."""
	path = ctx.root / AGENTS_FILE
	if not path.is_file():
		raise common.ToolError(f'not found: {AGENTS_FILE}')
	current = path.read_text()
	head, marker, _ = current.partition(AGENTS_MARKER)
	if not marker:
		raise common.ToolError(f'{AGENTS_FILE} has no marker line: {AGENTS_MARKER}')
	return path, current, f'{head}{marker}\n\n{listing}'


def main(ctx: common.Context, args) -> int:
	listing, problems = _listing(ctx)

	# A stale listing is reported, never written: splicing a listing built from
	# indexes that disagree with the tree would commit the disagreement.
	if not problems and (args.write or args.check):
		path, current, wanted = _spliced(ctx, listing)
		rel = path.relative_to(ctx.root).as_posix()
		if current == wanted:
			print(f'{rel} is up to date')
		elif args.write:
			path.write_text(wanted)
			print(f'{rel} updated')
		else:
			problems.append(f'{rel}: listing is stale, run `genvm-tool docs --write`')
	elif not (args.write or args.check):
		print(listing, end='')

	for problem in problems:
		print(problem, file=sys.stderr)
	return 1 if problems else 0
