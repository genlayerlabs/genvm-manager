"""
Tags a source file declares about its own case, as a `//` comment.

Shared by every plugin whose language spells comments that way, so `.rs` and
`.ts` cases declare tags identically and a typo is reported the same way.
"""

import re
from pathlib import Path

import genvm_tool.tests
from genvm_tool.tests import tags as test_tags

MARKER = 'genvm-tool-test-tags:'
_LINE = re.compile(r'^\s*//[/!]?\s*genvm-tool-test-tags:\s*(?P<tags>.*?)\s*$')


def source_name(ctx: genvm_tool.tests.stage.collection.Context, path: Path) -> str:
	try:
		return str(path.relative_to(ctx.shared.root_dir))
	except ValueError:
		return str(path)


def validate_declared(
	ctx: genvm_tool.tests.stage.collection.Context, path: Path, tags: list[str]
) -> list[str]:
	declared = frozenset(tags)
	registry = ctx.shared.tags_registry
	if registry is None:
		return sorted(declared)
	invalid = test_tags.unknown(registry, declared)
	if invalid:
		ctx.add_tag_offender(source_name(ctx, path), invalid)
	return sorted(declared - invalid)


def from_source(
	ctx: genvm_tool.tests.stage.collection.Context, path: Path
) -> list[str]:
	"""Extra tags declared by a `// genvm-tool-test-tags: a,b,c` comment."""
	tags: list[str] = []
	for line_number, line in enumerate(path.read_text().splitlines(), 1):
		if not line.lstrip().startswith('//') or MARKER not in line:
			continue
		match = _LINE.fullmatch(line)
		if match is None:
			ctx.add_tag_declaration_error(
				f'{source_name(ctx, path)}:{line_number}',
				f'malformed {MARKER} marker',
			)
			continue
		line_tags = [tag.strip() for tag in match.group('tags').split(',') if tag.strip()]
		tags.extend(line_tags)
	return validate_declared(ctx, path, tags)
