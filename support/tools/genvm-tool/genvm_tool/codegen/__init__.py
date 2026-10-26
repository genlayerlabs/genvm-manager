"""
`genvm-tool codegen` language backends.

A single front-end (:mod:`genvm_tool.codegen.model`) parses the codegen data JSON
into a typed model + shared string-trie; each language module renders that model.
Adding a language is just a new ``render(defs, **opts)`` registered in
:data:`BACKENDS`.
"""

from __future__ import annotations

from pathlib import Path

from . import go, python, rst, rust
from .model import Definition, load

BACKENDS = {
	'rust': rust.render,
	'python': python.render,
	'rst': rst.render,
	'go': go.render,
}

LANGS = tuple(BACKENDS)


def render(lang: str, defs: list[Definition], **opts) -> str:
	try:
		backend = BACKENDS[lang]
	except KeyError:
		raise ValueError(f'unknown codegen language {lang!r}') from None
	return backend(defs, **opts)


def generate(lang: str, input_path: Path, output_path: Path, **opts) -> None:
	"""Parse ``input_path``, render with ``lang``, write to ``output_path``."""
	Path(output_path).write_text(render(lang, load(input_path), **opts))


__all__ = ['BACKENDS', 'LANGS', 'generate', 'load', 'render']
