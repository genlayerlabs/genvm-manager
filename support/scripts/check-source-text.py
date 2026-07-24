#!/usr/bin/env python3
"""Pre-commit guard: keep source text plain and citation-free.

Comments should read on their own terms and stay ASCII. Rejected anywhere in a
scanned file: ADR references (`ADR-012`, `ADR 12`) and issue references
(`GVM-281`) -- they renumber and move, and that prose belongs in docs/, not a
source comment; and a blacklist of non-ASCII punctuation that each has an
obvious ASCII swap: the paragraph sign, em-dash (`--`), box-drawing horizontal
(`-`), rightwards arrow (`->`) and ellipsis (`...`). It is a blacklist, not a
full non-ASCII ban -- names and test data legitimately need other code points.

A file opts out entirely with a comment line (a `#`, `//` or `--` leader)
containing `check-source-text: off`; one such line skips the whole file.

Wired by the git-hooks `check-source-text` hook with the changed files as
arguments; the repo root is the working directory. Rejections go to stdout, one
per line, as `path:lineno: <the offending line>`.
"""

import argparse
import re
import sys

_OFF = re.compile(r'(?:#|//|--)[ \t]*check-source-text:[ \t]*off')

# The non-ASCII chars are spelled with `chr` so this file stays ASCII.
_PATTERNS = [
	re.compile(r'\bADR[ \-]?\d+'),  # ADR reference
	re.compile(r'\bGVM-\d+'),  # issue reference
	re.compile(chr(0x00A7)),  # paragraph sign
	re.compile(chr(0x2014)),  # em-dash (use two hyphens)
	re.compile(chr(0x2500)),  # box-drawing horizontal (use -)
	re.compile(chr(0x2192)),  # rightwards arrow (use ->)
	re.compile(chr(0x2026)),  # ellipsis (use ...)
]


def _violations(path: str):
	try:
		with open(path, encoding='utf-8') as f:
			text = f.read()
	except (OSError, UnicodeDecodeError) as e:
		yield 0, f'could not read: {e}'
		return
	if _OFF.search(text):
		return
	for lineno, line in enumerate(text.splitlines(), 1):
		if any(pat.search(line) for pat in _PATTERNS):
			yield lineno, line.strip()


def main(argv: list[str]) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument('files', nargs='*', help='files to check')
	args = parser.parse_args(argv)

	found = False
	for path in args.files:
		for lineno, content in _violations(path):
			found = True
			print(f'{path}:{lineno}: {content}')
	if found:
		print(
			'\nSource text must be plain ASCII and free of ADR/issue citations; '
			'add `check-source-text: off` in a comment to skip a file.',
			file=sys.stderr,
		)
		return 1
	return 0


if __name__ == '__main__':
	sys.exit(main(sys.argv[1:]))
